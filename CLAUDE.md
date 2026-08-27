# Clean Media for Jellyfin — orientation for Claude

Self-hosted, privacy-first detection of objectionable content in a user's own
media, with an **administrator review step before anything is acted on**.
Everything runs on the user's hardware — no cloud, no uploads. MIT licensed.

Full spec: [plan/prds/clean-media-prd.md](plan/prds/clean-media-prd.md).
In-Jellyfin review loop:
[plan/prds/2026-07-20-jellyfin-review-ui.md](plan/prds/2026-07-20-jellyfin-review-ui.md).
User-facing docs: [README.md](README.md). Current status: [PROGRESS.md](PROGRESS.md).
Feature catalog: [docs/FEATURES.md](docs/FEATURES.md).

## Where things stand (read [PROGRESS.md](PROGRESS.md) for the living detail)

The pipeline is built and tested end to end: worker + four engines (Phase 1),
Jellyfin plugin with segment provider and settings (Phase 2), and the whole
**in-Jellyfin review loop** — library grid, per-film findings editor, queueing
and live progress — code-complete across three slices with 102 tests (this is
Phase 4's "review UI inside Jellyfin"). Approved skips reach Jellyfin as
`Commercial` segments; renders apply mutes/blurs to a clean copy.

**The gap is deployment, not code.** None of the current build has been
exercised in a running Jellyfin: the boot worker may still serve older UI code
(restart with `install-service.ps1 -Restart`, elevated), the plugin needs
building/installing (`scripts/build-plugin.sh` + the repo manifest), and then
one film should be walked end-to-end in the real dashboard — grid → analyze →
progress → review → approve → confirm the skip during playback. Verify the
invariant, not the exit code. This deploy-and-verify step is the one thing
between the project and genuine daily use.

Near-term work discussed but not built (details in PROGRESS.md): one-click
"analyze for everything" (a job runs a single engine today, so profanity +
visual means two queue actions) with a real progress bar + ETA; a zoomed-in
**word-lock timing editor** (waveform + looped muted preview to place a mute on
the exact word); and **voice-only mute** — Demucs stem separation to drop the
spoken word while keeping music/ambient, windowed per finding, render-only.

## Ground rules

- **Keep the repo generic.** This project was deliberately scrubbed of any
  specific film titles (so it reads as applicable to any video, and can't be
  framed as targeting a studio's work). In committed files — code, tests,
  docs, commit messages — use generic examples like `Some Film (2010).mkv`,
  never real movie names. Real test files live in `movies/` (gitignored).
- **Nothing acts on a finding until an administrator approves it.** The AI
  proposes; the human disposes. Detection is tuned for recall; the review UI
  is where precision happens.
- **Verify the invariant, not the exit code.** The recurring failure mode in
  this project is a step that reports success while producing garbage (a cut
  that desyncs, a shot pass that skips 20% of the film, a worker serving stale
  code). After any pipeline step, check the thing it was supposed to produce.

## Layout

- `worker/` — FastAPI worker (the engine). `main.py` API, `models.py`,
  `policy.py`, `review.py`, `render.py`, `shots.py`, `store.py`, `batch.py`,
  `queue.py` (job queue), `schedule.py` (analysis-hours gating).
- `worker/engines/` — one adapter per detector behind a common interface
  (`base.py`): `subtitle_engine` (profanity from subs), `whisper_engine`,
  `vlm_engine` (Ollama Qwen3-VL vision), `pureframe_engine`, `vobsub` (OCR),
  `mute_render`, `voice_render` (Demucs voice-only mute), `profanity` (word
  lists), `subtitles` (SRT parse).
- `plugin/Jellyfin.Plugin.CleanMedia/` — the Jellyfin plugin (C#, net9.0).
- `scripts/` — every routine command (`.sh`, plus `install-service.ps1`).
- `manifest.json` — Jellyfin plugin-repository manifest at repo root.
- `plan/prds/` — the PRD and design docs.

## Running things (all via `scripts/`)

Python is managed with **uv** (`pyproject.toml`); scripts locate `uv`
themselves. `scripts/setup.sh` once, then:

- `scripts/worker.sh` — start the API on :8765.
- `scripts/analyze-audio.sh <paths>` — profanity only, ~1 min/film. Run this
  across the whole library first; it is ~250× faster than the visual pass.
- `scripts/analyze.sh <paths>` — profanity + visual (~hours/film, GPU-bound).
- `scripts/review.sh <film>` — open the review URL.
- `scripts/render.sh <film>` — render a clean copy from *approved* findings.
- `scripts/build-plugin.sh` / `release-plugin.sh` — package/publish the plugin.
- `scripts/install-service.ps1` — run the worker at boot on Windows.
- `scripts/install-service.sh` — run the worker at login on macOS (`launchd`).
- `scripts/build-dmg.sh` — package a double-clickable `.dmg` installer (macOS,
  wraps `install.sh` in a `.command` run from Terminal). See
  [worker/update.py](worker/update.py) for the check-and-prompt auto-update
  this pairs with.
- `scripts/build-pkg.sh` — package a native `.pkg` installer (macOS, Apple's
  own Welcome/License/Install wizard). `scripts/pkg/postinstall` runs as
  root and drops to the console user for anything Homebrew-related (Homebrew
  refuses to run as root). CI actually installs it on `macos-latest` and
  checks the worker comes up (see `.github/workflows/release-plugin.yml`),
  but that runner ships Homebrew pre-installed, so postinstall's
  fresh-Homebrew-prefix branch specifically stays unverified until this runs
  on a genuinely clean Mac — no such machine is available as of writing.

Tests: `uv run pytest`. **The sandbox blocks pytest's default temp dir**, so
pass `--basetemp` to a writable path when running here.

## Releasing (auto — just push to main)

**The worker and the plugin are one matched pair, released together.** They live
in this one repo and talk over an HTTP contract, so a version is a single git tag
`vX.Y.Z` containing *both*: the plugin published as `X.Y.Z.0` and the worker
stamped `X.Y.Z` (in [worker/__init__.py](worker/__init__.py)). To run a specific
version, check out its tag (worker) and install the matching plugin. This is how
a friend installs a compatible set without guessing.

A GitHub Actions workflow
([.github/workflows/release-plugin.yml](.github/workflows/release-plugin.yml))
auto-releases whenever **plugin source (`plugin/Jellyfin.Plugin.CleanMedia/**`)
*or* worker source (`worker/**`) changes on `main`**. It bumps the version (3rd
segment), builds the plugin, writes the release zip + `manifest.json`, stamps
`worker/__init__.py`, prepends to `CHANGELOG.md`, commits it back (`[skip ci]`),
and cuts a **GitHub Release** (tag + notes + zip). **Do not** hand-run
`release-plugin.sh` or bump the version for a normal change — just push; the
pipeline does it. (A worker-only change *does* now re-publish the plugin at the
new version — that lockstep is what keeps "check out `vX.Y.Z`, install plugin
`X.Y.Z.0`" true.)

**The two-number split.** `__version__` is the marketing/lockstep number that
bumps every release. Separately, `API_VERSION` (an int in `worker/__init__.py`,
mirrored by `SupportedWorkerApiVersion` in
[CleanMediaController.cs](plugin/Jellyfin.Plugin.CleanMedia/CleanMediaController.cs))
is the **HTTP-contract version** — bump it *only* when an `/api` change would
break an older plugin, and bump both sides together. The worker reports it at
`/api/health`; the plugin compares and shows a compatible / update-needed banner
on its settings page. So a slightly-behind plugin still works, and a real
mismatch is reported with the exact fix instead of failing silently.

**The one authored piece is the changelog, and that's on us (Claude):** when you
change the worker or the plugin, write the **user-facing** release note into
[plugin/CHANGELOG_NEXT.md](plugin/CHANGELOG_NEXT.md) in the same commit — plain
sentences about what changed for a *user*, not internal/testing detail. CI uses
it as the changelog (in the Release, `CHANGELOG.md`, and `manifest.json`), then
resets the file. If it's left empty, the release falls back to the commit
message. Docs-only changes don't trigger a release (and don't need a
`CHANGELOG_NEXT.md` entry). `release-plugin.sh` is still there for a manual
release; it reads the note from the `CHANGELOG` env var.

## Architecture essentials

- **Standard timeline format** — every engine emits the same segment shape
  (`startMs`/`endMs`/`category`/`confidence`/`engine`/`recommendedAction`/
  `approved`/`reasoning`). Stored per film in a `<name>.cleanmedia.json`
  sidecar, which is the source of truth for review decisions.
- **Observation/policy split** ([worker/policy.py](worker/policy.py)) — the
  VLM reports only what it *sees* (booleans: `female_topless`, `kissing`, …);
  `policy.py` decides what *counts*. Models don't obey negative instructions
  ("a shirtless man is NOT nudity" failed badly), and observations are cheap
  to re-interpret without re-running a multi-hour pass.
- **Review → approve → act** — `review.py` serves the admin UI and writes
  approvals to the sidecar. `GET /api/segments?path=…&approvedOnly=true` is
  what the plugin reads. The plugin reports approved *skips* to Jellyfin as
  `MediaSegmentType.Commercial`. Mutes/blurs need a rendered clean copy — no
  Jellyfin client applies them live.

## Hard-won gotchas (don't rediscover these)

- **DVD-rip telecine**: files advertise 29.97fps but decode at 23.976. Use
  PySceneDetect's `get_seconds()`, never frames÷fps. Guard shot coverage ≥95%.
- **ffmpeg cuts**: use `split`/`trim`/`concat`, not `select`+`setpts`; add
  `settb=1/1000` to avoid a 32-bit pts overflow ~35 min in.
- **Image subtitles**: pick the track by packet count (forced tracks are
  tiny); composite on **black then invert** before OCR; decode as UTF-8.
- **Whisper word timestamps are approximate** (a 0.3s word reports as ~0.7s),
  and whisper *sanitizes* profanity ("God"→"gosh"). For to-the-word mute
  timing: single-word cues use their own bounds; multi-word use fuzzy-matched
  ASR word spans, clamped, small pad. Truly exact timing needs forced
  alignment / CrisperWhisper (`timingModel` option) — some subtitle lines are
  simply mistimed vs the audio and no algorithm can place those.
- **VLM = Ollama Qwen3-VL, `-instruct` tags mandatory** (bare tags are
  thinking models and return no answer). 4B is the minimum usable size; 2B
  hallucinates wildly. **Confidence scores are uncalibrated** (0.98 on an ice
  cream cone) — never gate on them.
- **4 GB VRAM can't fully fit the 4B model** (weights ~3.3 GB). Keep
  `num_ctx` small (2048) + flash attention so most of it sits on GPU. Kill
  orphaned `llama-server.exe` processes — they steal VRAM and compute and are
  the usual cause of a slow/stalling visual pass. VLM requests retry+resume;
  the pass checkpoints every 25 samples.
- **Jellyfin 10.11 = net9.0**, relocated namespaces
  (`MediaBrowser.Controller.MediaSegments`, `Jellyfin.Database.…Enums`).
  Config page must not name a non-existent `data-controller`. Test the worker
  connection server-side, not from the browser (CORS).
- **Windows service** is a Task Scheduler task (S4U). It orphans an unkillable
  uvicorn child on stop, so **restart with `install-service.ps1 -Restart`**
  (elevated) — a bare Stop/Start leaves stale code serving on the port.
- **Worker reachability**: a Jellyfin server in a Docker bridge can't reach a
  Tailscale IP; use the LAN address.
