# Progress

Living status of the work. The formal spec and phase list live in
[plan/prds/clean-media-prd.md](plan/prds/clean-media-prd.md); the in-Jellyfin
review loop has its own spec in
[plan/prds/2026-07-20-jellyfin-review-ui.md](plan/prds/2026-07-20-jellyfin-review-ui.md).
This file tracks the current state and open threads. Keep entries generic — no
real film names (see [CLAUDE.md](CLAUDE.md)).

_Last updated: 2026-08-10._

## Where it's running now

The current build is **deployed and in active use**, not just built. The boot
worker serves current code; the plugin (**0.2.3.0**) installs from the repo
manifest and its film view + settings work; one film has been analyzed
(profanity: 26 findings; visual: 1) and **reviewed inside Jellyfin**; and the
media library has been reorganized to one folder per movie. Per-pass progress,
the review UI, the by-path render, and the waveform/filmstrip timing editors
have all been exercised against the real dashboard.

What's left to fully close Phase 4's "one film, end to end" on a live dashboard
(check the invariants, not exit codes):

1. ~~Restart the worker onto current code.~~ **Done** (`install-service.ps1
   -Restart`, elevated) — re-done after each worker change and the reorg.
2. ~~Build and install the plugin.~~ **Done** — 0.2.1.x installs from the repo
   manifest; "Clean Media Review" is in the dashboard menu.
3. **Confirm the two live invariants on a real film:** an approved **skip** is
   skipped during playback (needs a film with an approved skip — the one visual
   finding on the analyzed film was rejected), and a **rendered clean copy** has
   the muted word silenced with the original untouched (verified locally on a
   synthetic film; confirm on a real one via the film-view "Render clean copy").

### Jellyfin-side workflow gaps (discussed 2026-08-08)

The review page covers the whole loop, but a few things are missing for it to
feel like a product rather than a tool (details in conversation, to fold into
the review-UI PRD):

- **One-click "analyze for everything."** A job runs a single engine, so
  "profanity + bad scenes" means queuing twice (subtitles/whisper, then vlm).
  No combined/chained analyze. No per-card Analyze button either — only the
  bulk "Queue shown videos" gated by the filter.
- **Progress bar + ETA.** _Bar done (plugin 0.2.1.1); ETA still missing._ The
  grid and film view now show each in-flight pass with its engine label, live
  percent and a graphical bar, and the film view shows progress on open (no need
  to click Analyze). Fixed a display bug where a running pass hid behind a
  queued one of the same file (`/api/status` picked the newest-created job, so a
  queued profanity pass masked a running visual pass — it read as "stuck");
  status now returns every in-flight job per film, running first. Still missing:
  a time-remaining **ETA** — the worker records only `fraction` + `stage`
  (elapsed ÷ progress would give one).
- **Cancel button.** _Partly done (plugin 0.2.0.2)._ A bulk **"Cancel all
  analysis"** button now appears in the grid whenever a job is queued/running,
  backed by a new worker `POST /api/jobs/cancel-all` and cooperative
  cancellation (a running pass aborts at its next progress tick; the record is
  left `cancelled`, never resurrected). `DELETE /api/jobs/{id}` now cancels an
  active job rather than deleting a row the running pass re-creates. Still
  missing: a per-card cancel (story 40).
- **No subtitle → whisper fallback.** The default profanity engine finds
  nothing on a film with no subtitle track; there's no automatic fallback to
  the audio (whisper) pass.
- **Worker media roots — now wired to the NAS (2026-08-08).** The real library
  lives on the NAS (Jellyfin sees it as `/media/…`); the worker resolves those to
  local files by filename inside `CLEANMEDIA_MEDIA_ROOTS`, now pointed at
  `\\Nas\nas-8tb-hdd\Movies` and **verified reading it**. The task runs via
  `install-service.ps1 -AtLogon` (user's session → NAS access, no stored
  password). `resolve_media` caches an `os.walk` index (filename→path + which
  sidecars exist), rebuilt every 30 min and pre-warmed at startup. Open question
  for the generic plugin: how *anyone* points the worker at their media —
  direct/mounted (default), co-located worker, or an optional copy-locally mode
  for slow/flaky shares (see "Open decisions").

## Bugs (fix next)

Found while first exercising the plugin in a real Jellyfin (2026-08-08). Ordered
next-to-fix first. Tick when fixed and verified against the running dashboard.

- [x] **Unreachable worker is indistinguishable from "not analyzed."**
  `WorkerClient.GetFindingsAsync` returned `null` for *both* a 404 (film not
  analyzed) and an unreachable/timed-out worker, so the film view showed the
  misleading "No findings yet" (after a full timeout stall that reads as "Loading
  findings…" hanging), and the grid cards stuck on "checking…" forever because
  `loadStatus`'s unreachable branch never cleared them. **Fixed 2026-08-08:**
  `GetFindingsAsync` now returns a result that flags unreachability; `Findings`
  forwards an `unreachable` flag; the film view and every grid card now say
  "worker unreachable — check the Worker URL" instead of a false "no findings."
  Shipped in plugin **0.2.0.1**. _Verify:_ with a bad Worker URL, opening a film
  shows the unreachable message, not "No findings yet"; with the correct URL a
  test film's findings load. (The trigger in the wild was a mistyped Worker URL —
  `192.168.68.70` instead of the worker's `192.168.68.98` — a config error, but
  the UI must report it honestly rather than look like no findings exist.)
- [x] **No confirmation before "Queue shown videos."** One click queued a whole
  library (100+ films) for analysis with no guard — an expensive, easy-to-hit
  mistake. **Fixed 2026-08-08:** the button now opens a confirm dialog naming the
  count and engine before queueing. Shipped in **0.2.0.1**. _Verify:_ clicking
  Queue shows a confirm dialog; Cancel queues nothing.
- [x] **Plugin "Settings" opened the review grid, not the settings page.**
  Jellyfin 10.11's `findBestConfigurationPage` prefers the `EnableInMainMenu`
  page when a plugin has several, so the plugin's Settings button always landed
  on the review page — and 10.11's `PluginPageInfo` has no `IsMainConfigPage`
  flag to override it (only Name/DisplayName/EmbeddedResourcePath/
  EnableInMainMenu/MenuIcon/MenuSection). **Worked around 2026-08-08 (0.2.0.3):**
  the review page now carries a "⚙ Worker settings" button that navigates
  straight to the config page by name. _Verify:_ from the review page, the button
  opens the Worker URL / timeout settings.
- [x] **Grid "worker unreachable" on a NAS library (status timeout).** With the
  media root on a NAS, opening a folder (e.g. Marvel, 53 films) reported "worker
  unreachable" even though the connection test passed. Cause: `/api/status`
  read each film's sidecar with a **per-film stat over SMB, sequentially** —
  ~0.8s each × 53 = ~44s, past the plugin's 30s timeout. Also the media index
  used `Path.rglob + is_file()`, a stat per file, making the cold walk minutes.
  **Fixed 2026-08-08:** the index walk uses `os.walk` (no per-file stat) and now
  also records which sidecars exist, so `/api/status` answers "analyzed?" from
  memory — an unanalyzed page does **zero per-film NAS I/O**. Marvel status went
  44s → **1s** (measured on the real Jellyfin→worker path). A parallelization
  attempt was reverted — concurrent SMB stats made a home NAS *slower*. Worker
  also pre-warms the index at startup. _Verify:_ a NAS folder lists in ~1s.
- [x] **`install-service.ps1` false "worker did not answer".** Every restart
  reported the worker as down even though it was up. `/api/health` pings Ollama
  and takes ~2.1s; both health-check loops used a 2s timeout, so every poll
  raced and lost. **Fixed 2026-08-08:** both loops use a 15s timeout. Also added
  `-AtLogon` (run the worker in the user's session for network/NAS access with
  no stored password — a PIN can't be stored with a boot task) and a clearer
  `-UsePassword` prompt (Windows password, not the NAS login).

## Sliced roadmap

Ordered by what we want to prove, in order (set 2026-08-08): go **deep on one
film first** — review → skip → mute → to-the-word timing — then bad scenes, then
voice-only mute, and only **then** bulk queue/cancel. Analyzing a whole library
is explicitly *not* the first test; one film, reviewed properly, is. Each slice
is a thin vertical (worker + plugin + verify) that ends in something observable
on a real film — "done when" is the invariant to check, not the exit code.

**Starting fresh?** This section is the whole to-do list. Read
[CLAUDE.md](CLAUDE.md) for run commands, layout and gotchas;
[clean-media-prd.md](plan/prds/clean-media-prd.md) and
[the review-UI PRD](plan/prds/2026-07-20-jellyfin-review-ui.md) for the *why*.

**How to work this doc:** the next step is always the first unchecked slice,
marked **← NEXT**. Implement it, verify its _done when_ against a real film, tick
its box (`[ ]` → `[x]`), and move the **← NEXT** marker on.

- [x] **Slice 0 — Worker on current code.** Restarted the boot service with
  `install-service.ps1 -Restart` (elevated). _Done, verified 2026-08-08:_
  `/api/health` responds and the review page served on :8765 now matches the
  repo's current UI — the served `/api/review` grew from 11,065 chars (stale)
  to 17,165 and now carries the current markers (Type-filter row, bulk
  "Bad — act on all", "Play flagged part only") that the pre-restart build was
  missing. Confirmed against a real film's sidecar, not just the exit code.
- [x] **Slice 1 — Plugin into a running Jellyfin.** _Done, verified 2026-08-08._
  The release is published on GitHub (now `0.2.0.4`) and installs from the repo
  manifest
  (`https://raw.githubusercontent.com/danielmhair/jellyfin-clean-media/main/manifest.json`).
  "Clean Media Review" appears in the dashboard main menu and the settings
  **connection test passes** ("Connected. Worker 0.1.0 | engines: vlm,
  subtitles, pureframe, whisper | GPU: RTX 3050"). Worker URL is the LAN
  address `http://192.168.68.98:8765` (the Jellyfin server runs in Docker at
  `192.168.68.58`; a Tailscale IP would not route). `build-plugin.sh` now stamps
  `meta.json` from the csproj so a dev build never claims a stale version.
- [x] **Slice 2 — Per-film Analyze button.** _Done 2026-08-08 (plugin 0.2.0.4)._
  On the film detail page: an engine picker (Profanity fast / whisper / Visual) +
  an "Analyze this film" button that queues just that film via
  `POST /CleanMedia/Analyze`, shows live status ("Analyzing NN% — stage") by
  polling `/CleanMedia/Status`, and auto-loads the findings the moment it
  completes; the hours-long visual pass confirms first.
- [x] **Slice 3 — One film, end to end: review → skip + mute.** _Implemented
  2026-08-08 (plugin 0.2.1.0)._ The film view has a **"Render clean copy"**
  action: it POSTs `/CleanMedia/Render` → a new worker `POST /api/render?path=`,
  which renders from the film's **sidecar** (the source of truth for review
  decisions) via the combined renderer, acting on **approved findings only** —
  mutes/blurs plus any approved skips, folded into one clean copy under a
  `cleaned/` folder; the original is never touched. The button summarises what it
  will apply (N mutes/blurs/skips), polls live progress via
  `/CleanMedia/RenderStatus`, and reports where the copy was written. A by-path
  endpoint was needed because the plugin knows films by path, not job id — and
  the pre-existing `POST /api/jobs/{id}/render` renders the *store* timeline
  (analysis-time, approvals not reflected → it would mute every detection), so
  the by-path render reads the sidecar instead. Approved **skips** still reach
  playback live as `Commercial` segments (unchanged). _Verified locally:_ a real
  render through the worker path silences the approved mute span (−91 dB) while a
  **rejected** span and the rest of the audio stay intact (−21 dB) and the
  original file is byte-for-byte unchanged — the render honours review decisions,
  not just "not rejected". _Remaining (deploy step):_ install 0.2.1.0 into the
  running Jellyfin and walk one real film — approve a mute, click Render, confirm
  the clean copy; approve a skip, confirm playback skips it.
- [x] **Slice 4 — Word-lock timing editor (by ear, to the millisecond).**
  _Built 2026-08-09 (worker-side)._ The worker review page's read-only timing is
  now an editor: a new `GET /api/peaks` decodes the finding ±`PAD` (15 s) window
  to a downsampled waveform (ffmpeg → mono PCM → 40 peaks/s JSON, one peak per
  25 ms), which the page draws in a horizontally-scrollable canvas zoomed to
  ~160 px/s. Draggable start/end handles (snap 25 ms) plus ±25 ms nudge buttons
  and a live millisecond readout move the mute onto the exact word; **Preview
  muted** loop-plays the current selection via `/api/clip?mute=true` to tune by
  ear; **Save** persists through the existing `startMs`/`endMs` patch. _Verified:_
  `build_peaks` localizes a burst at the finding and the page renders the editor
  (tests); needs a worker restart to serve. _Done when (to confirm live):_ a
  reviewer drags a mute onto the exact word and the new bounds persist.
- [x] **Slice 5 — Bad scenes (visual): analyze → review → skip/blur + scene
  timing.** _Built 2026-08-09 (worker-side)._ The whole loop now exists for the
  visual pass. Review: the per-finding **mute/blur/skip selector** (0.2.1.3)
  lets a scene be set to skip (live) or blur (render). Retime: the timing editor
  is now **dual-mode** — visual findings (`vlm`/`pureframe`) get a **filmstrip**
  instead of a waveform. A new `GET /api/filmstrip` samples the finding ±`PAD`
  window at 1 frame/s and tiles them into one JPEG in a single ffmpeg call
  (160 px/frame = the editor's 160 px/s zoom, so a frame lines up with its real
  time); the same draggable handles / ±25 ms nudge / Save apply. Act: an
  approved skip is live (`Commercial` segment); an approved **blur** is applied
  by the combined renderer (`render.py` `gblur`) in the clean copy from Slice 3.
  _Verified:_ `build_filmstrip` returns a tiled JPEG and the page renders the
  filmstrip editor for visual findings (tests); needs a worker restart to serve.
  _Done when (to confirm live):_ a visual finding is reviewed, retimed on the
  filmstrip, and skipped or blurred.
- [x] **Slice 6 — Voice-only mute (remove the voice, keep the background).**
  _Built 2026-08-09 (worker-side)._ A new **"voice-only mute"** action drops the
  spoken word while music / Foley / ambient play straight through, so a swear
  over music no longer leaves an audible hole. Demucs (`htdemucs`, 2-stem)
  separates a padded ±1 s window around each finding; only the finding's own
  span has its vocals subtracted (mixture − vocals = accompaniment), the rest of
  the track untouched. New engine module
  [worker/engines/voice_render.py](worker/engines/voice_render.py): the render
  path (`render_voice_removed_pcm`) edits a full-length s16le track **in place
  via a memmap** — only the ~1 s window per finding is ever held as floats — and
  the combined renderer feeds it to FFmpeg as a second audio input in place of
  the source (`render.py` `build_command` gained `audio_path`). Review page: a
  **voice-only mute** option in the action selector plus a **"Play
  voice-removed"** preview (`/api/clip?voice=true`, `voice_removed_wav`) so a
  reviewer hears it before approving. Render-only (like mute/blur); the plugin
  reports only *skips* to Jellyfin, so nothing changed there. _Verified:_ 8 new
  tests cover the splice (only the span changes, accompaniment survives) and the
  FFmpeg wiring, all with an injected fake separator; and real Demucs was run
  end-to-end on a synthetic window (correct stem shape, untouched region
  byte-preserved). Needs a worker restart + `uv sync` (adds `demucs`). _Done when
  (to confirm live):_ a rendered clean copy drops a swear over music while the
  music plays through the gap.
- [x] **Slice 7 — Queue many + cancel (bulk).** _Built 2026-08-10 (plugin
  0.2.2.0)._ The two remaining plugin-side gaps are closed. **Per-card Analyze +
  Cancel on the grid** (story 40): each grid card now carries its own action
  button — **Cancel** while a pass is in flight (deletes each of that film's
  active jobs via `DELETE /CleanMedia/Jobs/{id}` → `DELETE /api/jobs/{id}`, which
  cancels queued *and* running), or **Analyze** otherwise; the button
  `stopPropagation`s so it doesn't also open the film. **One-action "analyze for
  everything"**: the card's Analyze (and a new **"Analyze everything"** button on
  the film view) queues every engine the film still needs — profanity (subtitles)
  + visual (vlm), one job each — skipping any already done or in flight
  (`neededEngines()`; either profanity engine satisfies the profanity slot, so a
  whisper-analyzed film isn't re-run with subtitles). ETA stayed client-side
  (server-side ETA left as the optional item). _Verified:_ Release build is clean
  (0 warnings) and the 125-test worker suite passes after `uv sync` (demucs 4.1.0
  now installed, so Slices 4–6 + the ±1s buttons go live on the next worker
  restart). _Done when (to confirm live):_ from the grid, a per-card Cancel stops
  that film's running pass and a per-card Analyze queues both engines — check the
  invariant (the pass actually stops), not the exit code.
- [ ] **Slice 8 — Anything else (polish & deferred PRD stories). ← NEXT** Grid sort by
  pending count (story 11), ± nudge buttons on a finding (story 26),
  preview-as-it-plays (story 28: a skip jumps over, a mute via
  `/api/clip?mute=true`), clear display of overlapping findings (story 35), and a
  subtitle → whisper fallback when a film has no subtitle track. _Done when:_
  each works against a real film.

## Built and validated

- **Worker + engine-adapter pipeline** — subtitles (profanity), whisper,
  VLM (vision), pureframe, vobsub OCR; standard timeline; SQLite store;
  per-film `.cleanmedia.json` sidecar; batch tool for library-scale runs.
- **Profanity pass** — reads the film's own subtitles (OCR-ing bitmap tracks),
  flags configurable word tiers, times each word to a tight mute window.
  ~1 min/film. Validated across the local test library.
- **Visual pass** — shot detection + VLM sampling with the observation/policy
  split. Hours/film (GPU-bound on a 4 GB card).
- **Review UI** — per-finding thumbnail + clip, approve/reject to the sidecar,
  exact-timing badge, "Play muted" preview (hear the scene as it will play),
  "flagged part only" toggle. Type filter + bulk approve/reject.
- **Jellyfin plugin** — installs from the repo manifest; reports approved
  skips as Commercial segments; server-side connection test.
- **Windows service** — Task Scheduler task runs the worker at boot;
  `install-service.ps1 -Restart` for clean code reloads.

## In flight

- **Visual re-run of two test films** with the tightened VLM prompt (v2),
  running detached and resumable (checkpoints every 25 samples). ~11–12
  s/sample, ~16 h total. A prior run died when the editor was closed
  mid-pass; the engine now retries per request and resumes from checkpoint,
  so an interruption costs minutes, not the whole run.

## Recent changes

### Merge findings into one segment (2026-08-10, worker)

- **Why:** the visual pass flags a scene *shot by shot* (e.g. a dance number as
  four adjacent "suggestive" findings) — a reviewer wants to skip the whole
  thing as one segment, not tick four.
- **Worker review page** — each finding card gets a **"merge"** checkbox; ticking
  2+ enables a **"Merge selected"** button (with an action picker, default skip).
  Merge POSTs to a new `POST /api/segments/merge`, which replaces the chosen
  findings with a single one spanning the earliest start to the latest end
  (`merge_into_one` in [worker/review.py](worker/review.py)). The merged finding
  is `MANUAL_ENGINE` (so a re-analysis won't erase it), an **approved skip** by
  default (merging is the decision), and takes the most severe source category.
  The page reloads after merge (ids/counts change). 3 new tests; 136 green.
- **Worker-only** — needs a worker restart to serve; no plugin change.

### Analysis schedule — run only during allowed hours (2026-08-10, worker + plugin 0.2.3.0)

- **Why:** a whole-library scan shouldn't hammer the GPU while someone's
  watching TV. The queue always existed; now it only *runs* during chosen hours.
- **Worker** — new [worker/schedule.py](worker/schedule.py): a per-weekday
  allowed-window `Schedule` (each day enabled + start/end minutes; end<start
  wraps past midnight; start==end = all day), persisted to `data/schedule.json`
  and cached in memory. `is_allowed(now)` is the gate. The queue
  ([worker/queue.py](worker/queue.py)) consults it two ways: it **holds** an
  analysis job before it starts until the window opens ("waiting for scheduled
  hours"), and for a **resumable** engine it **pauses mid-run** when the window
  closes — `_progress_cb` raises `JobPaused`, the job re-queues and resumes from
  the engine's checkpoint when the window reopens. Non-resumable passes (short
  profanity) are left to finish; renders are never gated. `EngineAdapter.resumable`
  (default False) is set True on the VLM engine (it already checkpoints every 25
  samples). New `GET/PUT /api/schedule` (returns a `ScheduleView` with an
  `allowedNow` snapshot). 8 new tests (is_allowed windows incl. wrap; queue hold,
  pause+resume, and non-resumable-not-paused) — 133 green.
- **Plugin (0.2.3.0)** — the settings page gains an **Analysis schedule** editor:
  a master "restrict to scheduled hours" toggle, a live allowed/paused status
  line (worker-local time), and seven day rows (enable + start/end time). It
  reads/writes the worker via a new `GET`/`POST CleanMedia/Schedule` proxy
  (`WorkerClient.Get/SetScheduleAsync`); the schedule lives only on the worker
  (single source of truth), the plugin is just the editor.
- **Needs a worker restart** to serve the new endpoints and enforcement; the
  plugin change ships in 0.2.3.0.

### Slice 7 — per-card Analyze / Cancel + one-action "analyze everything" (2026-08-10, plugin 0.2.2.0)

- **Per-card grid actions** — each film card in the review grid gets its own
  button: **Cancel** while any pass is in flight (fires `DELETE
  /CleanMedia/Jobs/{id}` for each active job — the worker cancels queued and
  running alike), or **Analyze** otherwise. The button `stopPropagation`s so a
  click acts on the card without also opening the film view. The bulk "Cancel
  all analysis" still stops the whole library; per-card stops just that film.
- **One-action "analyze everything"** — the card's Analyze, and a new **"Analyze
  everything"** button on the film-view launcher, queue every engine a film still
  needs (profanity `subtitles` + visual `vlm`) in one click, one job per engine.
  `neededEngines()` skips any engine already done or in flight; either profanity
  engine (subtitles or whisper) satisfies the profanity slot, so a whisper-run
  film isn't needlessly re-run with subtitles. The visual pass is still guarded
  by a confirm (GPU-heavy, hours).
- **Worker-side unblocked** — `uv sync` installed `demucs 4.1.0`, so Slices 4–6
  (waveform/filmstrip editors, voice-only mute) and the ±1s nudge buttons go live
  on the next worker restart. Plugin-only change otherwise; needs 0.2.2.0
  installed. Release build clean (0 warnings); 125-test worker suite green.

### Timing editor — ±1s coarse nudge (2026-08-10, worker)

- The waveform/filmstrip timing editor now has **±1s** nudge buttons on each
  handle alongside the existing **±25ms**, for findings whose timing is *way*
  off (a badly mistimed subtitle line). 1s jumps get a handle into the right
  neighbourhood; 25ms (and dragging) fine-tune to the word. Values stay on the
  25ms grid. Start/End split onto their own rows so eight buttons don't crowd.
- **Save timing persists to disk** — the editor's Save PATCHes
  `/api/segments/{id}` (by path) → `update_segment` → `save_timeline`, a full
  rewrite of the film's `.cleanmedia.json` sidecar (the source of truth every
  render and the plugin's `approvedOnly` read use). Only sent fields change, so
  retiming leaves approve/reject and the action untouched. _Known limit:_ the
  write is in-place, not write-temp-then-`os.replace`, so it isn't crash-atomic
  (fine for single-reviewer use; an easy follow-up if wanted).
- Reach is bounded by the editor window (finding ±15s / `CLIP_PAD_S`), so ±1s
  gives up to ~15s of travel each way — enough for the usual mistimings; a
  subtitle off by more than that would need a wider fetched window (deferred).
- **Worker-only** — needs a worker restart to serve; no plugin rebuild.

### Slice 6 — voice-only mute (drop the voice, keep the background) (2026-08-09, worker)

- **New engine module** [worker/engines/voice_render.py](worker/engines/voice_render.py)
  — Demucs `htdemucs` 2-stem separation, windowed. `separate_vocals` isolates
  the vocal stem of a ±1 s window (standard normalise → separate → denormalise,
  GPU with a CPU fallback for the 4 GB card); `remove_vocals` subtracts it
  **only** across the finding's span so the accompaniment carries through.
- **Render path** — `render_voice_removed_pcm` builds a full-length s16le audio
  track with the voice gone across every voice-only span, editing it **in place
  through a memmap** (only the window per finding is loaded as floats). The
  combined renderer (`render.py`) feeds it to FFmpeg as a second input in place
  of the source audio; `build_command` gained `audio_path`/`audio_sr`/`audio_ch`
  and the skip filter-graph now references the chosen audio input. Voice-only
  mutes force an audio re-encode but leave the video stream-copied.
- **Review UI** — a **voice-only mute** option in the per-finding action
  selector, and a **"Play voice-removed"** preview (`/api/clip?voice=true` →
  `voice_removed_wav` muxed over the clip) so the reviewer hears it first.
- **Dependency** — adds `demucs>=4.0` (downloads a model on first use). Worker
  restart + `uv sync` needed to serve it; the plugin is unchanged (voice-only is
  render-only, and only skips reach Jellyfin live).

### Slice 5 — visual timing via a filmstrip; scene skip/blur review (2026-08-09, worker)

- **`GET /api/filmstrip`** — one ffmpeg call samples the finding ±`PAD` window
  at 1 frame/s and tiles them into a single JPEG (`build_filmstrip`), so the
  browser makes one request, not one per thumbnail.
- **Dual-mode timing editor** — `vlm`/`pureframe` findings now open a
  **filmstrip** (drag the scene boundaries by eye); audio findings keep the
  waveform. Shared handles / ±25 ms nudge / ms readout / Save; preview adapts
  (muted for mutes, plain scene otherwise).
- Scene **skip** works live; scene **blur** renders via `render.py`'s existing
  `gblur` path in the Slice-3 clean copy. Worker-only; needs a worker restart.

### Library reorg — one folder per movie (2026-08-09)

- Moved **1,194 files** so every film sits in its own folder, nested in its
  collection (`Movies/<Collection>/<Film (Year)>/…`), across 11 collections
  (TV Shows and New left as-is). Same-volume renames, 0 failures; each film's
  companions (e.g. GotG2's `.cleanmedia.json` sidecar + engine files) travelled
  with it, so review decisions are preserved. Fully logged for one-command undo.
- After the move: the worker was restarted (rebuilds its filename→path index)
  and Jellyfin rescanned. The worker resolves by filename recursively, so the
  deeper layout is transparent to it.

### Slice 4 — waveform timing editor on the review page (2026-08-09, worker)

- **`GET /api/peaks`** — decodes a finding's ±`PAD` window to a downsampled
  waveform (ffmpeg → mono 8 kHz PCM → 40 peaks/s, one per 25 ms). The browser
  can't decode MKV, so the worker supplies the peaks as JSON with the window
  bounds. `build_peaks` in [worker/review.py](worker/review.py).
- **Timing editor** — the review page's timings were read-only text; now a
  "✎ Timing" button opens a scrollable waveform (zoomed ~160 px/s so 25 ms is a
  few pixels) with draggable start/end handles, ±25 ms nudge buttons, a live
  millisecond readout, **Preview muted** (loops the current selection via
  `/api/clip?mute=true`), and **Save** (existing `startMs`/`endMs` patch).
- **Worker-only change** — no plugin rebuild; needs a worker restart to serve.

### Film view → launcher; per-finding review moves to the worker page (2026-08-09, plugin 0.2.1.3)

- **Simplified the in-Jellyfin film view** to a launcher: film name + decided
  count, the 3 engine checkboxes + Analyze, live pass progress, a "Review
  findings (mute/blur/skip)" link, and Render clean copy. Removed the inline
  video player, the per-finding list/editor, "Next undecided" and "Add finding
  at playhead" — that per-item work now lives on the worker review page.
- **Worker review page gained a mute/blur/skip selector** per finding
  (PATCHes `recommendedAction`, re-renders so "Play muted" tracks it).
- **Analyze disabled unless an engine is ticked**; clearer render note that
  mutes/blurs need a rendered copy while skips work live.
- _Note:_ the review-page change is worker-side, so it needs a worker restart
  to serve; the film-view change ships in the plugin.

### Film-view polish (2026-08-09, plugin 0.2.1.2)

- **ETA** next to each pass's percent, estimated client-side from the rate of
  progress across polls (so it works without a worker restart; the worker still
  reports only `fraction`).
- **No re-queuing done/in-flight engines** — the film view disables an engine
  that is already running, queued, or completed. Worker `/api/status` now
  reports `enginesDone` per film; the plugin also infers "done" from findings
  already present, so it degrades gracefully on an un-restarted worker.
- **Fewer dead controls** — "Next undecided" is hidden until there are findings;
  "Add finding at playhead" only shows when the film actually plays (it needs a
  real playhead, which an un-transcodable MKV/DVD in the browser doesn't give).
- **Worker review-page link** — a button opens the worker's standalone
  `/api/review?path=` page for the film (via a new `GET /CleanMedia/Config` that
  hands the browser the worker URL), shown once the film has findings.

### Live analysis progress + running-not-hidden fix (2026-08-09, plugin 0.2.1.1)

- **Root cause of "queued but nothing happening"** — `/api/status` matched one
  job per file name by *newest created*. A film analyzed for two engines at once
  (visual + profanity) has two jobs; the profanity one, queued a moment later,
  masked the running visual pass, so the grid showed "queued" while a multi-hour
  pass was already 40%+ done.
- **Worker** — `/api/status` now returns **every** in-flight job per film
  (`jobs[]`, running/rendering first) plus a headline `job`; `JobBrief` carries
  its `engine`. Test added for the two-engine case.
- **Plugin (0.2.1.1)** — grid cards and the film view show each pass with its
  engine label, live percent and a progress bar; the film view starts showing
  progress on open (no need to click Analyze) and loads findings the moment the
  last pass finishes.

### Slice 3 — render a clean copy from the film view (2026-08-08)

- **By-path render endpoint** — new worker `POST /api/render?path=` renders a
  clean copy from the film's `.cleanmedia.json` sidecar, acting on **approved
  findings only** (mutes/blurs + any approved skips) via the combined
  `render.py`. Runs as a background job through the existing queue
  (`submit_media_render`/`_render_media`); progress polled via `/api/jobs/{id}`.
- **Fixed a review-invariant hole** — the older `POST /api/jobs/{id}/render`
  rendered the *store* (analysis-time) timeline, whose approvals are always
  null, so `render_muted`'s `approved is not False` would have muted **every**
  detection. The by-path render reads the sidecar and filters to
  `approved is True` (shared `approved_for_render`, now used by
  `scripts/render.sh` too). New tests assert an unreviewed film renders nothing.
- **Plugin (0.2.1.0)** — "Render clean copy" button in the film view: confirms,
  POSTs `/CleanMedia/Render`, polls `/CleanMedia/RenderStatus`, shows live
  percent/stage and the written path; the button is disabled with a hint until a
  mute or blur is approved (skips already work live).
- **Cheap duration** — `shots.media_duration` (one ffprobe, no decode) supplies
  the length skips need without `true_fps`'s full decode-and-count; mute/blur
  renders skip the probe entirely.
- **Verified locally** — a real render silences the approved mute span (−91 dB),
  leaves a rejected span and the rest of the audio intact (−21 dB), and does not
  touch the original file.

### Deploy & first-run-in-Jellyfin session (2026-08-08)

- **Deployed and proven reachable end to end** — worker restarted onto current
  code (Slice 0), plugin published and installed from the repo manifest (Slice
  1), grid lists the real NAS library. Plugin releases `0.2.0.1`–`0.2.0.4`.
- **Media roots on the NAS** — worker now reads `\\Nas\nas-8tb-hdd\Movies` via
  `-AtLogon`; `os.walk` index (filename→path + sidecar set), pre-warmed, 30-min
  TTL. Fixed a 500 when a root is unreachable (`is_dir()` raising over SMB).
- **Grid status made NAS-fast** — `/api/status` no longer stats each film's
  sidecar over SMB; Marvel (53 films) went 44s → 1s, clearing the "worker
  unreachable" timeout.
- **Per-film Analyze button** (Slice 3) — engine picker + live progress on the
  film page, auto-loads findings when done.
- **Cancel** — "Cancel all analysis" button + `POST /api/jobs/cancel-all`, with
  real cooperative cancellation; `DELETE /api/jobs/{id}` cancels active jobs.
- **Honest unreachable + Settings reachable + Queue confirm** — see Bugs above.
- **`install-service.ps1`** — `-AtLogon`, 15s health-check timeout (was falsely
  reporting "did not answer"), clearer `-UsePassword` prompt.

### Word-timing / VLM session

- **Word-timing precision** — cut mute padding (200→70 ms) and the minimum
  window (400→240 ms); single-word cues now use tight ASR word timing rather
  than the full subtitle-display span. Median mute dropped ~0.8 s → ~0.48 s.
- **Fewer estimates** — single-word-cue bounds + fuzzy word matching
  (`god's`≈`god`, `asses`≈`ass`) + clamp-don't-reject recovered most words
  that used to fall back to a character-offset guess.
- **VLM prompt v2** — clears the known visual false positives (clothed people
  in tight/unusual outfits, near-kisses, flesh-coloured sculptures) while
  keeping recall. Distant/dark nudity in a low-res frame remains a documented
  model-capability limit, not a prompt bug.
- **VLM resilience** — per-request retry with backoff; skip-and-resume on
  isolated stalls; abort only on persistent Ollama failure, with progress
  saved.
- **Service restart fix** — `install-service.ps1 -Restart` ends the task and
  kills the orphaned uvicorn child that a bare Stop/Start leaves on the port.

## Proposed next (discussed 2026-08-08)

- **Word-lock timing editor** — a zoomed-in fine-tuner for when a mute lands
  off the word. Zoom the player to the finding ±~1.5 s, draw a waveform of
  that window (a swear word is a visible burst), and let the reviewer drag the
  start/end handles; each change loops playback of just the selected span
  *with the mute applied*, so they tune by ear until the word is gone and the
  words either side survive. Saves through the existing `startMs`/`endMs`
  patch — no new persistence. This is the human answer to the timing problem
  no ASR can solve (subtitle lines mistimed against the audio; see CrisperWhisper
  note below). One new worker piece needed: a **peaks endpoint** (ffmpeg → PCM
  → downsampled peaks JSON) for the padded window, because the browser can't
  decode MKV to build a waveform. The `/api/clip?mute=true` preview already
  supplies the audio to loop.

- **Voice-only mute** — remove the word but keep the background. Today a mute
  silences *all* audio for the span, so a swear over music or crowd noise
  leaves an audible hole. Instead, source-separate the audio around the finding
  into vocals + accompaniment (Demucs `htdemucs`, 2-stem), zero **only** the
  vocals across the mute span, and remix — music, Foley and ambient play
  through; just the spoken word drops. **Windowed**: separate only ~2–3 s
  around each finding and splice back, never the whole track, so it stays fast
  and cheap. Slots into `mute_render` as a new mode ("voice-only" vs "hard
  mute"); still **render-only**, so Jellyfin can't apply it live. Caveats:
  separation isn't perfect — faint vocal bleed can leave a ghost or a slight
  timbre shift in the background for the span, and a second person talking
  simultaneously drops too (same stem); usually subtle at ~0.4 s. Biggest
  payoff is the case that sounds worst today — a word over music. The same
  separation also enables a better review preview ("Play voice-removed"
  alongside "Play muted"). Dependency: Demucs (torch already present) + a
  model download (~few hundred MB).

## Open decisions / pending

- **CrisperWhisper for timing** — the ~4 remaining wide words are either
  whisper-sanitized ("God"→"gosh", "whore"→"horse") or on subtitle lines
  whose timestamps point at the wrong audio. CrisperWhisper (verbatim, sharper
  boundaries) would fix the sanitized ones; it's already supported via the
  `timingModel` option but needs a ~1.5 GB model download. Awaiting a call on
  whether to install it. Subtitle-mistimed lines can't be fixed by any ASR.
- **GPU throughput** — the 4 GB card caps the visual pass at partial-GPU
  speed. A card with ≥6 GB (or offloading Ollama to one) would fit the 4B
  model fully and also allow a more accurate 8B.

## Known limits

- Jellyfin can only *skip* during playback; mutes/blurs require a rendered
  clean copy.
- VLM confidence scores are uncalibrated — treat every finding as "needs
  review", never auto-apply.
