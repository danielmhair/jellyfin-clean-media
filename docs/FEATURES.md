# Clean Media for Jellyfin — Feature list

A capability catalog for the project. For the *why* see the
[PRD](../plan/prds/clean-media-prd.md) and the
[review-UI PRD](../plan/prds/2026-07-20-jellyfin-review-ui.md); for current
status and open threads see [PROGRESS.md](../PROGRESS.md); for run commands and
gotchas see [CLAUDE.md](../CLAUDE.md).

Everything runs on the user's own hardware — no cloud, no uploads. An
administrator reviews and approves every finding before anything is acted on:
**the AI proposes, the human disposes.**

---

## Detection engines

One adapter per detector behind a common interface, so every engine emits the
same timeline shape and adding one never changes the plugin.

| Engine | Detects | Action | When to use |
|---|---|---|---|
| **`subtitles`** | profanity (from the film's own subs, OCR-ing bitmap tracks) | mute | Default for audio; ~1 min/film |
| **`vlm`** | nudity, sexual activity, kissing (Ollama Qwen3-VL vision) | skip / blur | Default for video; GPU-bound, hours/film |
| `whisper` | profanity (speech recognition) | mute | Fallback when a film has no subtitle track |
| `vobsub` | profanity (OCR of image subtitles) | mute | Bitmap/DVD subtitle tracks |
| `pureframe` | nudity, sexual activity | blur | Not recommended (high false-positive rate) |

**Profanity detail** — configurable word tiers (always-on swearing, optional
mild register, optional blasphemy, custom words); each word timed to a tight
mute window using ASR word spans, fuzzy word matching, and clamp-don't-reject
recovery. Subtitles beat speech recognition decisively when a track exists (a
human already transcribed the dialogue).

**Visual detail** — shot detection + per-shot VLM sampling with an
**observation/policy split**: the model reports only what it *sees* (booleans
like `female_topless`, `kissing`), and [policy.py](../worker/policy.py) decides
what *counts* — so re-interpreting what's objectionable is instant, without
re-running a multi-hour pass. Long runs checkpoint every 25 samples and resume.

---

## Standard timeline

Every engine converts its output to one shape
(`startMs`/`endMs`/`category`/`confidence`/`engine`/`recommendedAction`/
`approved`/`reasoning`), stored per film as a `<name>.cleanmedia.json` **sidecar**
next to the media — the source of truth for review decisions, surviving
independently of the worker database. Also mirrored in a SQLite store.

---

## Analysis scheduling

- **Per-weekday allowed windows** — restrict analysis to chosen hours so a
  library scan never hammers the GPU while someone's watching TV. Each day has
  its own enable + start/end; a window may wrap past midnight; start == end means
  all day.
- **Hold and resume** — the queue holds an analysis job before it starts until
  the window opens, and a resumable pass (the VLM) **pauses mid-run** when the
  window closes and resumes from its checkpoint when it reopens. Renders are
  never gated.
- **Timezone-aware** — windows are evaluated in an IANA timezone captured from
  the admin's browser (via `zoneinfo`), so the schedule follows the person who
  set it up rather than the worker box's clock (which may be UTC in a container).

---

## Job queue

- **Single-GPU serial queue** — one background worker processes jobs in order;
  duplicate analysis of identical media is detected by fingerprint and reused.
- **Restart-resilient** — jobs persist to SQLite, so on startup the queue
  re-enqueues everything unfinished **in submission order** and resumes; a pass
  caught mid-run is re-run (the VLM from its checkpoint) and stale `running`
  ghosts from a prior crash are rescued.
- **Cooperative cancellation** — cancel one job or all in flight; a running pass
  aborts at its next progress tick and is recorded `cancelled`, never
  resurrected.

---

## Review UI (worker page)

The administrator's review surface, served by the worker at `/api/review`.

- **Per-finding cards** — thumbnail, a browser-playable clip padded ±15 s
  seeking to the flagged part, category, confidence, engine, reasoning, and an
  exact-vs-approximate timing badge.
- **Approve / reject** — two buttons per finding, written straight to the
  sidecar; `suggestive` findings are surfaced as "needs your call" questions.
- **See the cut** — a marker strip over the preview highlights the flagged span
  (what gets cut/muted/blurred) with a playhead.
- **Preview skip** — plays the run-up then jumps over the span the way Jellyfin
  skips it live, so you watch the skip, not just the scene.
- **Preview muted / voice-removed** — hear the scene as it will play once acted
  on, before approving.
- **Timing editor** — dual-mode: a **waveform** for audio findings, a
  **filmstrip** for visual ones (chosen by category, so a duplicated or merged
  scene still opens frames). Draggable start/end handles (25 ms snap), ±25 ms and
  ±1 s nudges, typed `H:MM:SS.mmm` times (millisecond precision on the card and
  in the fields), and a live readout. Drag across the strip to **audition** any
  span — it shows its exact time and offers "Use as bounds" to set the finding to
  what you just watched. The window **follows the edited times**: a "↻ Frames
  here" button re-anchors the strip around the current bounds (so a finding moved
  far away doesn't show a stale/blank strip), and Save resets the window to 15 s
  either side of the new bounds.
- **Editable description** — the finding's note/reasoning is editable in place
  (e.g. to fix the stale shot reference a duplicate carries), and Add-segment
  takes an optional description.
- **Per-finding action** — set each finding to mute / voice-only mute / blur /
  skip.
- **Add / duplicate / delete / merge** — add a finding the engines missed,
  duplicate one to reuse elsewhere (retime and re-describe the copy), delete
  noise, or merge several adjacent detections (a scene flagged shot-by-shot) into
  one segment.
- **Filters & bulk** — filter by decision state or by type (each profane word
  its own group), and bulk-approve/reject exactly what's shown.

---

## Jellyfin plugin (C#, net9.0)

- **Segment provider** — implements `IMediaSegmentProvider`; reports approved
  **skips** as `MediaSegmentType.Commercial`, which supporting clients skip
  during playback. No rendering, no second copy. Mute/blur segments are filtered
  out (no client can apply them live) and flagged as needing a rendered copy.
- **In-Jellyfin review loop** — a library grid and per-film launcher: analyze
  (per-engine or one-click "analyze everything"), live per-pass progress with an
  engine label and bar, per-card and bulk cancel, a link to the worker review
  page, and "Render clean copy".
- **Settings page** — worker URL with a server-side connection test (reports
  version, engines, GPU), analysis-hours schedule editor, timeout.
- **Honest failure** — an unreachable worker is reported as such, never as "no
  findings"; installs from the repo `manifest.json`.

---

## Rendering a clean copy

For players that can't skip, and for mutes/blurs that can't be applied live.

- **Approved-only** — renders from the sidecar's approved findings; rejected and
  undecided are ignored; refuses to run if nothing is approved. The original is
  never touched.
- **Combined pass** — blur, skip and mute folded into one FFmpeg pass to a
  `cleaned/` copy. Mute-only renders stream-copy the video (bit-for-bit picture,
  minutes); blur/skip force a re-encode (NVENC when available).
- **Voice-only mute** — Demucs 2-stem separation drops the spoken word while
  music / Foley / ambient play through the gap, windowed per finding and edited
  in place via memmap. Render-only.

---

## Operations (self-hosting)

- **Windows boot service** — `install-service.ps1` registers a Task Scheduler
  task that runs the worker at boot (before login), restarts it on failure, and
  reaches a NAS share via the user's session (`-AtLogon`, no stored password).
  Launched hidden (no console window); `-Restart` reloads new code cleanly.
- **Structured logging** — one readable format to console and a rotating file
  (`data/logs/worker.log`); a startup banner, a request log (routine polling at
  DEBUG, mutations/errors and slow requests surfaced), and full job lifecycle
  with progress and durations. `CLEANMEDIA_LOG_LEVEL` / `CLEANMEDIA_LOG_FILE`
  tune it.
- **NAS-aware path resolution** — the plugin knows a film by its Jellyfin mount
  path; the worker resolves it to a local file by name via a cached `os.walk`
  index (filename→path + which sidecars exist), pre-warmed at startup and
  refreshed on a TTL, so a library page does near-zero per-film NAS I/O. A
  short negative-lookup cache spares the NAS from a playback client polling an
  unanalyzed film.
- **Batch tool** — `worker.batch` for library-scale runs from the CLI.

---

## Reliability & correctness guards

Hard-won guards against *plausible-but-wrong* output (the recurring failure mode
in this project):

- **Shot-coverage / telecine** — DVD rips advertise 29.97 fps but decode at
  23.976, so PySceneDetect's timeline runs short. `detect_shots` **rescales** its
  timeline onto the reliable ffprobe duration (full coverage, no partial
  timeline); a genuinely truncated decode still fails loudly.
- **Render sync** — cuts use `split`/`trim`/`concat` with `settb=1/1000` to
  avoid a 32-bit pts overflow ~35 min in; skips drop subtitle tracks (cutting
  would desync them).
- **Image-subtitle selection** — pick the dialogue track by packet count (forced
  tracks are tiny), composite on black then invert before OCR.
- **VLM discipline** — `-instruct` model tags only; 4B minimum; confidence
  scores are uncalibrated, so nothing auto-applies — every finding needs review.
- **Approval invariant** — nothing reaches Jellyfin or a render until an
  administrator approves it; detection is tuned for recall, review is where
  precision happens.
