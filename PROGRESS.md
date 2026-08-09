# Progress

Living status of the work. The formal spec and phase list live in
[plan/prds/clean-media-prd.md](plan/prds/clean-media-prd.md); the in-Jellyfin
review loop has its own spec in
[plan/prds/2026-07-20-jellyfin-review-ui.md](plan/prds/2026-07-20-jellyfin-review-ui.md).
This file tracks the current state and open threads. Keep entries generic — no
real film names (see [CLAUDE.md](CLAUDE.md)).

_Last updated: 2026-08-08._

## The real blocker: nothing is running the current build

Everything below is **built and tested but not actually deployed and exercised
end-to-end in a running Jellyfin.** This is the one thing standing between the
project and genuine daily use — see the phase roadmap in
[clean-media-prd.md](plan/prds/clean-media-prd.md) (Phase 4 "review UI inside
Jellyfin" is code-complete) and the status header of
[the review-UI PRD](plan/prds/2026-07-20-jellyfin-review-ui.md) ("all three
slices built … not yet loaded into a running Jellyfin").

To get it running properly, in order:

1. ~~**Restart the worker onto current code.**~~ **Done 2026-08-08** via
   `install-service.ps1 -Restart` (elevated). The boot service had been serving
   older UI code; it now serves the current build — verified by the served
   `/api/review` page carrying the current markers the stale build lacked, not
   just by `/api/health` answering. (A bare Stop/Start leaves a stale uvicorn on
   the port — see [CLAUDE.md](CLAUDE.md).)
2. **Build and install the plugin into the running Jellyfin.**
   `scripts/build-plugin.sh` then install from the repo manifest; confirm the
   "Clean Media Review" entry appears in the dashboard main menu.
3. **Walk one film end-to-end in the real UI** — library grid → queue analysis
   → watch live progress → review findings → approve → confirm Jellyfin skips
   the approved span during playback. Verify the invariant, not the exit code.

Until steps 1–3 are done and observed, treat every "built" item below as
"built, unverified in production."

### Jellyfin-side workflow gaps (discussed 2026-08-08)

The review page covers the whole loop, but a few things are missing for it to
feel like a product rather than a tool (details in conversation, to fold into
the review-UI PRD):

- **One-click "analyze for everything."** A job runs a single engine, so
  "profanity + bad scenes" means queuing twice (subtitles/whisper, then vlm).
  No combined/chained analyze. No per-card Analyze button either — only the
  bulk "Queue shown videos" gated by the filter.
- **Progress bar + ETA.** Cards show a live text percentage but no graphical
  bar and no time-remaining; the worker's progress callback records only
  `fraction` + `stage`, never an ETA (elapsed ÷ progress would give one).
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
- **Worker only sees `movies/`.** The real library lives on the NAS
  (`/media/…`, `/volume1/…` as Jellyfin sees it); the worker resolves those to
  local files by filename inside `CLEANMEDIA_MEDIA_ROOTS`. `resolve_media` now
  caches a filename→path index (walk once per 5 min, not per lookup) so a large
  root is usable, but the root still has to be pointed at media reachable from
  the worker machine (re-run `install-service.ps1 -MediaRoots …`, `-UsePassword`
  for a UNC/SMB share). Pending the actual share path.

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

## Sliced roadmap

Ordered so each slice is independently grabbable and ends in something
observable — "done when" is the invariant to check, not the exit code. Deploy
first (slices 0–2); nothing else matters until the current build is proven in a
real Jellyfin. Features and deferred PRD stories after (slices 3–10), each a
thin vertical (worker + plugin + verify).

**Starting fresh?** This section is the whole to-do list. Read
[CLAUDE.md](CLAUDE.md) for run commands, layout and gotchas;
[clean-media-prd.md](plan/prds/clean-media-prd.md) and
[the review-UI PRD](plan/prds/2026-07-20-jellyfin-review-ui.md) for the *why*.
Slices 9–10 are review-UI PRD stories that were deferred, not built. Story
numbers below refer to that PRD.

**How to work this doc:** the next step is always the first unchecked slice,
marked **← NEXT**. Implement it, verify its _done when_ against a real film,
tick its box (`[ ]` → `[x]`), and move the **← NEXT** marker to the following
slice. That is what "implement the next step in PROGRESS.md and update it after"
means. Slices 0–2 are deploy-and-verify and need an operator with elevated
access and a running Jellyfin; slice 3 onward is code an agent can write.

- [x] **Slice 0 — Worker on current code.** Restarted the boot service with
  `install-service.ps1 -Restart` (elevated). _Done, verified 2026-08-08:_
  `/api/health` responds and the review page served on :8765 now matches the
  repo's current UI — the served `/api/review` grew from 11,065 chars (stale)
  to 17,165 and now carries the current markers (Type-filter row, bulk
  "Bad — act on all", "Play flagged part only") that the pre-restart build was
  missing. Confirmed against a real film's sidecar, not just the exit code.
- [ ] **Slice 1 — Plugin into a running Jellyfin. ← NEXT** The `0.2.0.0` release
  is now **published and verified live** on GitHub (pushed 2026-08-08): the raw
  manifest's newest version is `0.2.0.0` and its zip returns HTTP 200 with a
  matching checksum, so Jellyfin can install it straight from the repo. Add the
  repository in Dashboard → Plugins → Repositories:
  `https://raw.githubusercontent.com/danielmhair/jellyfin-clean-media/main/manifest.json`,
  install Clean Media, restart Jellyfin. (The plugin C# is unchanged since
  `0.2.0.0`, so no rebuild/bump is needed; `build-plugin.sh` now stamps its
  `meta.json` from the csproj, so a local dev build no longer claims a stale
  version.) _Done when:_ "Clean Media Review" appears in the dashboard main menu
  and its settings connection test passes (test server-side; use the LAN
  address, not a Tailscale IP, if Jellyfin runs in a Docker bridge).
- [ ] **Slice 2 — Prove the loop on one film.** Grid → queue analysis → watch live
  progress → review findings → approve one → play the film. _Done when:_
  Jellyfin skips the approved span during playback on a real client.
- [ ] **Slice 3 — Per-video Analyze button.** Finishes the unbuilt half of story 36
  in the review-UI PRD (today's queueing is bulk-only, via the filter). Plugin:
  hover a poster card → an Analyze button on that card → queues just that one
  film; while it runs the card shows its own progress. No worker change — the
  single-film submit path already exists (`POST /api/jobs`, one media path).
  _Done when:_ hovering one unanalyzed film and clicking Analyze queues that
  film and nothing else, and the card transitions to "analyzing".
- [ ] **Slice 4 — "Analyze for everything" (both engines).** Worker: let a submit
  queue both the profanity and visual passes for a film (chain, or accept a
  list of engines). Plugin: a Quick / Deep / Both choice on the per-card
  Analyze action from slice 3. _Done when:_ choosing "Both" on one film
  produces profanity and visual findings without a second action.
- [ ] **Slice 5 — Progress bar + ETA + cancel.** Worker: add an ETA to
  `JobBrief`/`MediaStatus` (elapsed ÷ progress). Plugin: swap the text percent
  for a `<progress>` bar with time-left, and wire the existing cancel endpoint
  to a grid button (story 40). _Done when:_ a running visual pass shows a moving
  bar with a plausible time-left, and Cancel stops it.
- [ ] **Slice 6 — Subtitle → whisper fallback.** Worker: when a film has no
  subtitle track, fall back to the whisper audio pass (or surface a distinct
  "no subtitles" state) instead of reporting zero findings. _Done when:_ a
  subtitle-less film still gets profanity findings from audio.
- [ ] **Slice 7 — Word-lock timing editor.** Worker: a peaks endpoint (ffmpeg →
  PCM → downsampled JSON) for a padded window. Plugin: zoom the finding to
  ±~1.5 s, draw the waveform, drag start/end handles, loop-play the selected
  span *with the mute applied*; save via the existing `startMs`/`endMs` patch.
  _Done when:_ a reviewer can move a mute onto the exact word by ear and the
  new bounds persist to the sidecar.
- [ ] **Slice 8 — Voice-only mute.** Worker: Demucs 2-stem separation on a padded
  window around a finding, zero the vocals across the mute span, remix; add it
  as a mode in `mute_render` and a "Play voice-removed" preview. Plugin: expose
  "voice-only" as an action. _Done when:_ a rendered clean copy drops a swear
  word over music while the music plays through the gap. Render-only.
- [ ] **Slice 9 — Deferred review-UI PRD stories (editor & grid polish).** Small,
  independent items left from the review-UI PRD, all plugin-side:
  - Story 11 — sort the grid by pending count, so the quickest films clear
    first.
  - Story 26 — nudge a finding's start/end by small increments (± buttons),
    to fine-tune without typing a timestamp.
  - Story 28 — preview the segment as it will actually play: for a skip, jump
    over it; for a mute, `/api/clip?mute=true` (the worker already supports it).
  - Story 35 — show overlapping findings clearly, so a reviewer merges or trims
    rather than stacking redundant skips.
  _Done when:_ each story's UI works against a real film in the dashboard.
- [ ] **Slice 10 — Render a clean copy from the review UI** (out of scope in the
  review-UI PRD, but required before any mute/blur takes effect). Worker render
  already exists (`POST /api/jobs/{id}/render`, `scripts/render.sh`); this wires
  a "Render clean copy" action into the film view and surfaces render progress.
  _Done when:_ approving mutes/blurs on a film and clicking Render produces a
  clean copy with those actions applied, original untouched. Render-only actions
  are inert until this exists.

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

## Recent changes (this session)

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
