# PRD — Distributed Workers & Queue Resilience

> **Status: not started. Design approved 2026-08-25.**
> Created 2026-08-25. ✅ implemented and verified · ⏳ in progress · ⬜ not started.
>
> Extends [clean-media-prd.md](clean-media-prd.md) and the queue built in
> [2026-08-14-queue-manager-and-in-plugin-settings.md](2026-08-14-queue-manager-and-in-plugin-settings.md).
> That queue runs **one job at a time on one machine**; this document turns it
> into a small distributed job system that keeps **every CPU and GPU on every
> machine busy**, and that **never silently stops**.

---

## Problem Statement

Two problems, one theme: hardware sits idle, and the queue sometimes dies quietly.

**1. The queue is single-slot on a single box.** [worker/queue.py](../../worker/queue.py)
opens with its own summary — *"Single-GPU job queue: one background worker thread
processes jobs in order."* There is one `job-worker` thread, one `_running_id`,
and `_select_runnable()` picks exactly one job. The observed consequences with a
deep queue (27 whisper + 31 VLM + renders):

- While whisper runs on the local GPU, the **second GPU (dangaming2nd) sits idle**
  unless the running job itself happens to be VLM.
- CPU-bound passes that never touch the GPU — profanity-from-subtitles
  (~1 min/film), VOB/PGS OCR, and ffmpeg renders — **wait in line behind GPU
  jobs** for no reason.
- The remote machine's **CPU is never used at all**; it is reachable only as an
  Ollama endpoint.

**2. It stops running and no one is told.** The Windows service restarts only on
*process death* ([install-service.ps1](../../scripts/install-service.ps1),
`-RestartCount 999`). It does **not** catch a worker that is alive but wedged, nor
the known orphaned-uvicorn case where a zombie holds the port so Task Scheduler
believes all is well. In practice the queue "sometimes stops," and the only way
to find out today is to notice nothing is progressing.

## Solution

Turn the single in-process queue into a **coordinator + nodes** system with a
**lease-based pull queue**, and make **resource** (not just "the GPU") the thing
we schedule against.

- One **coordinator** owns the authoritative queue, the schedule, the sidecar
  truth, and the review API the plugin talks to. It runs on the primary PC.
- Every machine — the primary PC *and* dangaming2nd — runs a **node agent** that
  advertises its free resources (`gpu`, `cpu` slots), pulls jobs it can run,
  executes engines locally, and streams progress back. Both nodes run the **same
  worker code**; "coordinator" is just a role flag.
- A job is matched to a node with a free resource of the class it needs, so a
  node runs (for example) **whisper on its GPU and a profanity pass on its CPU at
  the same time**, while the other node runs **VLM on its GPU** — three passes at
  once across two machines.
- Resilience is built into the same mechanism: a job is handed out under a
  **lease**; if a node dies mid-job the lease expires and the coordinator
  re-queues it (resumable engines resume from checkpoint). On top of that, an
  **internal supervisor** re-arms wedged executors and aborts hung jobs, and an
  **external watchdog** on each machine restarts a dead/zombie agent — all
  **schedule-aware**, so "nothing running" outside the analysis window is never
  mistaken for a fault.

Everything added to the HTTP contract is **additive**, so a slightly-behind
plugin keeps working; `API_VERSION` bumps only if an existing shape changes.

## User Stories

**Using all the hardware**

1. As an operator, I want whisper, a profanity pass, and VLM to run at the same
   time across my two machines, so a library scan finishes in a fraction of the
   wall-clock it takes today.
2. As an operator, I want CPU-only passes (profanity, OCR, render) to run
   alongside GPU passes on the same box, so a fast job never waits behind a
   multi-hour one.
3. As an operator, I want the "Running now" panel to show **every** job running
   across both machines, each labelled with the node it is on, so I can see the
   whole fleet at a glance.

**Not stopping silently**

4. As an operator, I want the plugin's status pill to tell me when a node has
   gone quiet or a job has stalled, so I learn it stopped from the UI, not from
   noticing nothing changed.
5. As an operator, I want a wedged or hung job to be recovered automatically
   (re-queued or aborted with a reason), so one bad film cannot freeze the queue.
6. As an operator, I want a dead agent restarted on its own machine within a few
   minutes, so a crash self-heals without me logging in.
7. As an operator, I want all of this to respect the analysis schedule, so the
   watchdog stays quiet (about analysis) outside the allowed hours while still
   flagging a genuinely-down server.

## Architecture

### Roles

- **Coordinator** (primary PC): owns the SQLite store, the schedule, lease
  bookkeeping, the review UI/API the plugin proxies to, and sidecar writes (the
  source of truth). Also runs a local node agent.
- **Node agent** (every machine, incl. the primary): a loop that reports free
  resources, claims a matching job under a lease, runs the engine locally,
  streams progress, posts the result, releases the lease. Identical code on every
  node; a `-Role`/config flag and a `CoordinatorUrl` select behaviour.

Generalize to **N nodes** from the start (a node registry), so a third machine is
config, not a rewrite.

### Lease-based pull queue

The coordinator's dispatcher stops being an in-process job *runner* and becomes a
**lease manager**:

- `POST /api/nodes/claim` — body: `{nodeId, resources:{gpuFree, cpuFree}}`.
  Returns the lowest-position runnable job whose resource class fits, marks it
  `running` with `{nodeId, leaseExpiresAt}`, or `204` if nothing fits. Analysis
  claims are **schedule-gated** here (renders never are).
- `POST /api/jobs/{id}/progress` — `{fraction, stage}` from the node; **renews
  the lease** and updates the job (reuses today's `_progress_cb` semantics).
- `POST /api/jobs/{id}/result` — the node posts the `Timeline` (analysis) or
  `renderedPath` (render); coordinator writes DB + sidecar and marks
  `completed`/`rendered`, releases the resource.
- `POST /api/jobs/{id}/fail` — `{error}`; coordinator marks `failed`.
- **Lease sweeper** — a coordinator thread re-queues any `running` job whose
  `leaseExpiresAt` passed (node died / network dropped). Resumable engines (VLM)
  resume from checkpoint; others restart. This is the distributed half of
  resilience.

Why pull, not push: a node that dies simply stops pulling and its lease lapses —
no coordinator-side connection to detect, no half-delivered job. It is the robust,
boring pattern.

### Resource model

Each node advertises, per its config:

- `gpu`: integer count of GPUs (usually 1).
- `cpu`: a bounded number of concurrent CPU slots (default ~= cores/2; the render
  sub-cap lives here — **max 1 concurrent render per node** to avoid thrashing
  the NAS, even if more CPU slots are free).

Job → resource class (drives claim matching):

| Engine | Class | Notes |
|---|---|---|
| `whisper_engine` | gpu (cpu fallback) | CUDA; can run on CPU far slower — allow as a last resort only |
| `vlm_engine` | gpu | runs against the **node's own local Ollama** in node mode |
| `pureframe_engine` | gpu | visual model |
| `subtitle_engine` (profanity) | cpu | the ~1 min/film pass |
| `vobsub` (OCR) | cpu | |
| `mute_render` / `render` / `render_media` | cpu | ffmpeg; render sub-cap applies |
| `voice_render` (Demucs) | gpu-preferred | Demucs benefits from GPU; classify gpu, cpu fallback |

VLM in node mode is a simplification win: instead of one job fanning across all
hosts, **two VLM jobs run — one per node, each on its own GPU**. Higher throughput
and simpler failure handling. The existing multi-host fan-out
([vlm_engine.py `_hosts()`](../../worker/engines/vlm_engine.py)) stays valid for a
single-node deployment.

### Media & checkpoint access (the sharp edges)

- **Both nodes must read the media and write clean copies.** dangaming2nd must
  mount the same library (`\\Nas\nas-8tb-hdd\Movies`). The coordinator stores a
  canonical path; each node maps it to its own mount via the existing
  `resolve_media` mount-mapping, extended to a **per-node media-root map**.
- **Sidecar stays coordinator-written.** Nodes return the `Timeline`; the
  coordinator writes the DB row and the `<name>.cleanmedia.json` sidecar (today's
  `store.save_timeline` path), so there is exactly one writer of truth.
- **Resumable checkpoints must be reachable by whoever resumes.** VLM's plan
  checkpoint is a local file today. Either (a) write checkpoints to **shared
  storage** (next to the sidecar on the NAS) so any node can resume, or (b) **pin
  a resumed job back to the node that holds its checkpoint.** Option (a) is
  cleaner and preferred; flagged as a risk to validate.

## Resilience (Feature B, in the distributed frame)

Three layers, each catching what the others cannot:

1. **Lease sweep (coordinator)** — node died mid-job → lease lapses → re-queue.
2. **Internal supervisor (each node)** — a thread that re-arms a dead executor
   and **aborts a job that exceeds a generous per-engine wall-time with no
   `updatedAt` advance** (VLM checkpoints every 25 samples; whisper reports
   coarsely — thresholds set high to avoid false aborts), freeing the slot and
   failing the job with a clear reason so one hung film cannot wedge the node.
3. **External watchdog (each machine)** — a small scheduled task pings the local
   agent's `/health` every ~3–5 min. **Healthy** = agent answers *and* its
   heartbeat is fresh. On unhealthy it reclaims the port (today's
   `Stop-WorkerProcesses` already kills orphaned `uvicorn`/`llama-server`) and
   restarts the agent — the `install-service.ps1 -Restart` path factored into a
   tiny `watchdog.ps1`.

**Schedule-awareness (all layers):** a *progress* check only applies when
`scheduleAllowedNow` is true and there is queued analysis work; outside the
window, "no analysis running" is expected and never a fault. A **server that does
not answer `/health` is always a fault**, in-hours or not.

### Node recovery — the second node going down

The three layers above recover a node's *work* (lease sweep) and a node's *local
agent* (its own watchdog). This fourth layer is the **coordinator recovering a
whole node** when the local watchdog cannot — the box is up but the agent is
unreachable, or the machine itself is unreachable.

- **Detect + degrade first, react second.** When a node's heartbeat goes stale
  the coordinator marks it `offline`, stops assigning it jobs, and re-queues its
  leased work to surviving nodes (a remote-GPU-only VLM job falls back to the
  local GPU — slower, but it runs). **The fleet keeps working degraded; a down
  node never stalls the coordinator.**
- **Don't over-react to a blip.** A stale heartbeat is first treated as transient
  — retry `/health` with backoff over a short grace window before declaring the
  node down, so a network hiccup or a GC pause does not trigger a restart.
- **Escalating recovery attempts**, each tried in order with backoff and capped:
  1. **Reconnect** — keep polling `/health`; a node whose agent self-healed
     locally simply re-registers and starts pulling again (no coordinator action
     needed — the common case).
  2. **Remote agent restart** — if the box answers but the agent does not, the
     coordinator triggers a restart on the node via a pre-agreed, authenticated
     hook (Task Scheduler remote trigger / WinRM `Restart-ScheduledTask` /
     SSH). This is a machine-to-machine credential set up at install time.
  3. **Wake / power** — if the box itself is unreachable, attempt Wake-on-LAN
     (opt-in; only meaningful if the node is merely asleep).
- **Give up loudly, not silently.** After N failed attempts the coordinator stops
  retrying, marks the node **down** in `/api/health` and the status pill with the
  last-seen time and the reason, and leaves the fleet running on the remaining
  nodes. Recovery must never turn into a restart loop that masks a real fault.
- **Schedule-aware, like everything else.** A node idle *because analysis hours
  are closed* is `idle`, not `offline`; only a stale **heartbeat** (the agent
  isn't answering at all) triggers the recovery ladder.

The node's **own external watchdog remains the primary, most-reliable recovery**
(a local restart needs no network); coordinator-driven recovery is the backstop
for when the node can't fix itself.

**Heartbeat:** the coordinator dispatcher and every node agent write a heartbeat
timestamp each loop (exposed on `/health`). Staleness is the single signal the
supervisor, watchdog, and UI all read.

## API & UI changes

- `GET /api/health` gains `nodes: [{id, resources, heartbeatAt, running:[{id,
  engine, progress, updatedAt}]}]`, `scheduleAllowedNow`, and a fleet-wide
  `runningCount`. Additive → no `API_VERSION` bump.
- **Review page "Running now"** ([reviewPage.html `classify()`](../../plugin/Jellyfin.Plugin.CleanMedia/Configuration/reviewPage.html)):
  show **all** `running` jobs plus active renders concurrently (drop the
  single-active assumption `runningAll.length ? runningAll : renderingAll`), each
  row **labelled with its node**. "Up next" excludes every running job. The
  `#CmCActive` counter already renders a live number.
- **Status pill** (`TestConnection`): show `N running`, a per-node up/down dot,
  and a **stall/quiet warning** when a heartbeat is stale — so "not running" is
  visible at a glance instead of discovered by absence of change.

## Deployment

- `install-service.ps1` gains `-Role coordinator|node` and `-CoordinatorUrl`.
  The primary PC installs as `coordinator` (also runs a local node); dangaming2nd
  installs as `node -CoordinatorUrl http://<primary-LAN>:8765`.
- dangaming2nd prerequisites: the repo + `uv` env + models (`scripts/setup.sh`),
  Ollama already present, ffmpeg, and the **NAS mounted at the same library
  path**. Remember the Docker/Tailscale reachability gotcha — nodes talk to the
  coordinator over the **LAN** address, not a Tailscale IP.
- The external watchdog task is registered per machine as part of install.

## Risks / open questions

- **Checkpoint sharing (a) vs node-pinning (b)** for resumable VLM — validate
  that writing the plan checkpoint to the NAS beside the sidecar is fast/robust
  enough; if not, pin resumes to the owning node.
- **Media-root mapping across nodes** must be exact, or a node 404s every job —
  reuse and extend `resolve_media`; test with a real NAS mount on dangaming2nd.
- **Stall-abort thresholds** are the dangerous knob — a false abort on a slow-but-
  fine VLM pass is worse than a late catch. Start generous, tune down.
- **CPU slot sizing** — too many CPU slots and renders/whisper-on-CPU starve the
  box; default conservative (cores/2, render sub-cap 1) and expose in config.
- **Store contention** — the per-op lock in [store.py](../../worker/store.py) is
  already safe for concurrent writers; verify no read-modify-write races appear
  under real fleet load (esp. `patch_segment`/sidecar merge).
- **Remote-restart credential** — coordinator-driven node recovery needs a
  machine-to-machine hook (WinRM/SSH/remote Task Scheduler) with stored trust
  between the boxes; scope it least-privilege (restart that one task, nothing
  more) and confirm it survives the S4U/interactive login model the service uses.
- **Recovery must not loop** — cap attempts and back off; a restart loop that
  keeps "fixing" a genuinely broken node hides the fault instead of surfacing it.

## Rollout (tracer-bullet slices, to be expanded by `to-slices`)

1. **Resource-aware local concurrency** — single machine, multiple lanes:
   coordinator+local-node in one process, `gpu`/`cpu` slots, concurrent
   Running-now. Delivers "use both CPU and GPU on this box" and the multi-active
   UI. No network yet.
2. **Lease-based queue + resilience** — leases, lease sweep, internal supervisor,
   external watchdog, schedule-aware health, status-pill stall warning. Delivers
   "never silently stops," still single-machine.
3. **Second node (dangaming2nd)** — node-mode agent, node registry, per-node
   media-root map, VLM-per-node, `-Role`/`-CoordinatorUrl` install, NAS mount,
   node labels in the UI. Delivers "both machines fully utilized."
4. **Node recovery** — coordinator node-supervisor: stale-heartbeat detection
   with a grace window, degrade + re-queue to survivors, the escalating recovery
   ladder (reconnect → remote agent restart → wake), capped attempts, and a
   loud `down` state in `/api/health` and the status pill. Delivers "if the
   second node stops, we try to recover it."
