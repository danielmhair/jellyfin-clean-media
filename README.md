# Clean Media for Jellyfin

Self-hosted, privacy-first detection of objectionable content in your own
media library, with an administrator review step before anything changes.

**Originals are never modified.** Everything runs on your hardware — no cloud
APIs, no telemetry, no uploads.

---

## Contents

- [How it works](#how-it-works)
- [Detection engines](#detection-engines)
- [Install](#install)
- [Scripts](#scripts)
- [Running the worker](#running-the-worker)
- [Analyzing a film](#analyzing-a-film)
- [Reviewing findings](#reviewing-findings)
- [Rendering a clean copy](#rendering-a-clean-copy)
- [The Jellyfin plugin](#the-jellyfin-plugin)
- [Worker API](#worker-api)
- [Timeline format](#timeline-format)
- [Gotchas worth knowing](#gotchas-worth-knowing)
- [Development](#development)

---

## How it works

```
            +-----------------------+
            |      Jellyfin         |   Clean Media plugin
            |    (NAS / server)     |   - IMediaSegmentProvider
            +-----------+-----------+   - skips approved scenes
                        |
              HTTP over Tailscale/LAN
                        |
            +-----------v-----------+
            |   Clean Media Worker  |   Python + FastAPI
            |   (GPU workstation)   |   queue, store, render
            +-----------+-----------+
                        |
    +----------+--------+--------+-----------+
    |          |                 |           |
 subtitles   vobsub OCR        vlm        whisper
 (profanity) (profanity)  (visual, Ollama) (profanity)
```

The **worker** analyzes local files and produces an engine-agnostic
*timeline* of findings. You review those findings and approve or reject each
one. Approved findings can then either be **rendered into a clean copy**, or
served to Jellyfin so approved scenes are **skipped during playback**.

Every detector is an adapter behind one interface, so adding an engine never
changes the plugin.

---

## Detection engines

| Engine | Detects | Action | Use it when |
|---|---|---|---|
| **`subtitles`** | profanity | mute | **Default for audio.** The film has any subtitle track — text or bitmap. |
| **`vlm`** | nudity, sexual activity, kissing | skip / blur | **Default for video.** Needs Ollama; runs anywhere on your network. |
| `whisper` | profanity | mute | Last resort: no subtitle track at all. |
| `pureframe` | nudity, sexual activity, kissing | blur | Not recommended — see below. |

### Measured results

Numbers from testing this repo against several full-length films, each
scored by hand against ground truth. Titles are omitted deliberately — the
evidence is in the failure modes, not the films.

**Profanity — subtitles beat speech recognition, decisively.**

| Test film | Subtitle/OCR engine | Whisper `medium.en` |
|---|---|---|
| A (126 min, text subtitles) | **9 of 9** | 4 of 9 |
| B (138 min, bitmap subtitles) | **12 found** | 7 found |
| C (121 min, text subtitles) | **28 found** | not run |

Whisper's misses aren't mistiming — the words are absent from its transcript
entirely. On film A, 3 of 5 misses were transcribed as pure silence (the
voice-activity filter discarding speech under music), and 2 were
hallucinated or truncated sentences. On film B it missed the two strongest
lines in the script outright. Subtitles win because a human already
transcribed the dialogue; the job is reading it, not guessing it.

**Visual — a general VLM beats a purpose-built detector.**

On film A, PureFrame flagged **855 shots** — 44% of the runtime — of which
essentially all were wrong; its two "explicit nudity" hits were a courtroom
scene and an explosion. `qwen3-vl:4b-instruct` flagged the one genuine
scene and **nothing else** across the control shots, describing it
accurately.

**Model size is a cliff, not a gradient.** `qwen3-vl:2b-instruct` flagged a
jellyfish, a starfish, a handwritten note and glass vials as nudity at
0.95–0.97 confidence. No threshold rescues that. **Use 4B or larger.**

---

## Install

Requirements:

| Tool | Why | Install |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python toolchain + deps | `winget install astral-sh.uv` |
| FFmpeg | everything | must be on `PATH` |
| [Ollama](https://ollama.com) ≥ 0.12.7 | `vlm` engine | needed only for visual detection |
| [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) | OCR of DVD subtitles | `winget install UB-Mannheim.TesseractOCR` |

```bash
scripts/setup.sh    # installs dependencies and reports any missing tool
scripts/test.sh     # verify: python tests + plugin build
```

### Ollama setup for the `vlm` engine

```bash
ollama pull qwen3-vl:4b-instruct
```

Use an **`-instruct`** tag. The bare `qwen3-vl:4b` tag is a *thinking* model
that spends hundreds of tokens reasoning before answering — pure latency for
a yes/no judgement, and it returns empty content if generation is capped.

To run inference on a different machine, set `OLLAMA_HOST=0.0.0.0:11434` on
that box, restart Ollama, and allow port 11434 through its firewall. Then
point the engine at it with the `host` option. Note that `OLLAMA_HOST` also
retargets the CLI, so use `127.0.0.1` in shells where you run `ollama pull`.

**VRAM guidance:** `qwen3-vl:4b` is 3.3GB and fits a 6GB card with headroom.
`qwen3-vl:8b` and `gemma4:e4b` are ~6.1GB and will *not* fit 6GB alongside
context — Ollama silently falls back to CPU, costing 5–20× speed. Check with
`ollama ps`: if `size_vram` is 0, you are on CPU.

---

## Scripts

Everything routine lives in [`scripts/`](scripts/) so no command survives
only in someone's shell history. They work in Git Bash on Windows as well
as Linux, and locate `uv` themselves — it installs to `~/.local/bin`, which
is not on `PATH` in a fresh shell.

| Script | Purpose | Typical time |
|---|---|---|
| `setup.sh` | Install dependencies, re-apply pureframe patches, report missing tools | 2 min |
| `test.sh` | Python test suite, then build the plugin | seconds |
| `worker.sh` | Start the worker API on :8765 | — |
| `analyze-audio.sh <paths>` | **Profanity only** | **~1 min/film** |
| `analyze.sh <paths>` | Profanity **and** visual | ~4 h/film |
| `review.sh <film>` | Print/open the review URL for a film | — |
| `render.sh <film>` | Render a clean copy from *approved* findings | 10–40 min |
| `build-plugin.sh [dir]` | Build and package the Jellyfin plugin | seconds |
| `install-service.ps1` | Windows only: run the worker at boot (see below) | seconds |
| `organize-library.ps1` | Windows only: give every movie its own folder (see below) | seconds |

### A typical run

```bash
scripts/setup.sh                       # once
scripts/worker.sh &                    # leave running

scripts/analyze-audio.sh movies/       # whole library, minutes
scripts/analyze.sh "movies/Some Film (2010).mkv"   # add visual for one film

scripts/review.sh "movies/Some Film (2010).mkv"    # approve/reject in browser
scripts/render.sh "movies/Some Film (2010).mkv"    # clean copy, if you want one
```

**Start with `analyze-audio.sh` across everything.** It is roughly 250×
faster than the visual pass and gives complete, accurate profanity results,
which tells you which films are worth hours of GPU time.

### Options

Both analysis scripts accept files, folders or globs, skip work already
done, and never re-ingest a rendered `(Clean)` copy.

```bash
# run the vision model on another machine
OLLAMA_HOST_URL=http://100.95.155.5:11434 scripts/analyze.sh movies/

# a different model
VLM_MODEL=qwen3-vl:8b-instruct scripts/analyze.sh movies/

# a different port for the worker
PORT=9000 scripts/worker.sh

# tell the worker where films live, so Jellyfin's paths resolve
CLEANMEDIA_MEDIA_ROOTS=/mnt/movies:/mnt/tv scripts/worker.sh
```

`render.sh` refuses to run if nothing is approved — review comes first, by
design. Nothing is ever written over the original.

---

## Running the worker

```bash
scripts/worker.sh
```

Or with Docker:

```bash
docker build -t clean-media-worker .
docker run -p 8765:8765 -v /path/to/media:/media clean-media-worker
```

Check it: `curl http://localhost:8765/api/health` reports version, GPU,
installed engines and queue depth.

### Keeping it running (Windows)

The plugin returns no segments whenever the worker is unreachable, and it
does so silently — playback just stops skipping. If Jellyfin is always on,
the worker should be too.

From an **elevated** PowerShell in the repo root:

```powershell
.\scripts\install-service.ps1
.\scripts\install-service.ps1 -MediaRoots "D:\Movies;D:\TV" -Port 8765
.\scripts\install-service.ps1 -Restart      # after pulling new worker code
.\scripts\install-service.ps1 -Uninstall
```

This registers a scheduled task that starts at boot — before anyone logs
in — and restarts the worker every minute if it dies. It is Task Scheduler
rather than a real service because uvicorn is a console program: making it
a true service needs NSSM or WinSW wrapped around it, for the same
behaviour.

The script starts the task and then polls `/api/health`, so it tells you
whether the worker actually came up rather than just that the task
registered. It prints the LAN addresses to use as the plugin's Worker URL.

| Where | What |
|---|---|
| Log | `%LOCALAPPDATA%\CleanMedia\worker.log` (truncated past 10 MB) |
| Launcher | `%LOCALAPPDATA%\CleanMedia\worker-service.cmd`, regenerated on install |
| Task | `CleanMediaWorker` in Task Scheduler |

By default the task runs under your account with no stored password
(`S4U`), which means it has **no network credentials**. That is fine for
local media roots. If `-MediaRoots` points at a UNC path or mapped drive,
add `-UsePassword` and the script will prompt for and store your Windows
password with the task.

```powershell
Get-Content "$env:LOCALAPPDATA\CleanMedia\worker.log" -Tail 40   # what went wrong
Get-ScheduledTask CleanMediaWorker | Get-ScheduledTaskInfo       # last run result

# restart it after pulling new worker code (elevated)
.\scripts\install-service.ps1 -Restart
```

A stale worker serving old code looks exactly like a bug in the new code —
restart after every `git pull`. Use `-Restart`, **not** a bare
`Stop-ScheduledTask; Start-ScheduledTask`: ending the task leaves the uvicorn
process it spawned orphaned on the port, and that orphan runs in a service
context only an elevated `taskkill` (which `-Restart` does for you) or a
reboot can clear.

### One folder per movie

Clean Media writes up to ten sidecars next to each video — the timeline,
the shot list, the transcript, the censor plan, the progress file. In a
flat library that is thousands of files in one directory, with each film's
sidecars interleaved with every other film's. Jellyfin recommends a folder
per movie anyway; the sidecars make it worth doing.

```powershell
.\scripts\organize-library.ps1 -Path "\\NAS\Media\Movies"           # dry run
.\scripts\organize-library.ps1 -Path "\\NAS\Media\Movies" -Apply
```

A file joins a movie when its name starts with that movie's name and a
dot, so `.cleanmedia.json` and `.eng.srt` travel with the video. Where two
films could claim a file the longer name wins, which is what keeps
*Iron Man 2 (2010)*'s sidecars away from *Iron Man (2008)*.

Nothing is deleted or overwritten, and files are moved rather than copied,
so on one volume a 1200 film library takes seconds. Every move is logged
as it happens, and the log replays backwards:

```powershell
.\scripts\organize-library.ps1 -Undo -LogPath "\\NAS\Media\organize-20260721-140233.csv" -Apply
```

Re-running is safe and cheap: only the top level is scanned, so films
already in folders are untouched, and a sidecar written later — a new
`.srt`, or analysis run before organizing — joins its film rather than
being stranded. Add `-IncludeCleaned` to move rendered copies from a
shared `cleaned\` folder into each movie's own.

Afterwards, point the worker at the library root and let it find films by
name: `CLEANMEDIA_MEDIA_ROOTS` is searched recursively, so nesting needs
no configuration change.

---

## Analyzing a film

Submit a job through the API:

```bash
curl -X POST http://localhost:8765/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"mediaPath": "/media/Some Film (2010).mkv", "engine": "subtitles",
       "options": {"includeMild": true, "includeBlasphemy": true}}'
```

Poll `GET /api/jobs/{id}` for progress. Identical media is fingerprinted, so
re-submitting the same file returns the existing job rather than re-analyzing.

### Engine options

**`subtitles`** — reads the film's own subtitle track, OCR-ing it if the
track is bitmap-based.

| Option | Default | Meaning |
|---|---|---|
| `includeMild` | `false` | also flag the kid register — stupid, idiot, poop, jerk |
| `includeBlasphemy` | `false` | also flag God, Jesus, Christ as exclamations |
| `extraWords` | `[]` | your own additions |
| `wholeCue` | `false` | mute the whole subtitle line instead of the word |
| `preciseTiming` | `true` | re-transcribe cue audio to locate the exact word |
| `language` | `eng` | subtitle language to prefer |

Swearing — including *hell*, *damn*, *ass* and *prick* — is always flagged
and needs no option. `includeMild` is for the register below that.

**`vlm`** — samples frames per shot and asks a vision model about them.

| Option | Default | Meaning |
|---|---|---|
| `model` | `qwen3-vl:4b-instruct` | must be an `-instruct` tag |
| `host` | `http://localhost:11434` | Ollama endpoint; may be another machine |
| `maxGapS` | `2.5` | max seconds between samples inside a long shot |
| `minSamples` | `2` | frames per shot, even short ones |
| `padShots` | `1` | extend each flagged scene outward by N shots |
| `bridgeShots` | `2` | merge flagged shots separated by ≤N unflagged shots |
| `action` | `skip` | `skip` or `blur` |
| `flagMaleShirtless` | `false` | flag every bare male chest, not only sexualised framing |
| `flagUnderwear` | `true` | flag underwear and lingerie |
| `flagAnyKissing` | `false` | flag all kissing, not only the private kind |

The model is never asked whether something is objectionable — only what it
can **see** (`female_topless`, `male_shirtless`, `sexualised_framing`, …).
[`worker/policy.py`](worker/policy.py) turns those observations into
findings.

That split matters. Told "a shirtless man is NOT nudity", the model flagged
50 shots of a shirtless character as nudity anyway, one of them described
as *"no explicit nudity"* — negative instructions fight the category label
and the label wins. It also means changing what counts re-derives findings
from stored observations instantly, instead of costing another four-hour
pass over the film.

Long runs checkpoint after every 25 samples and on every detection — rerun
the same job to resume. Checkpoints record the model and prompt, and are
discarded if either changes, so a weak model's verdicts can never leak into
a stronger model's run.

---

## Reviewing findings

```bash
scripts/review.sh "movies/Some Film (2010).mkv"
```

Opens a page listing every finding with a thumbnail and **a playable clip
padded 15 seconds either side**, seeking to where the flagged part begins —
a still frame cannot show whether a scene is objectionable, motion and
context decide it. Two buttons per finding: **Bad — act on it** and
**Fine — ignore**.

Findings start undecided. **Nothing is skipped in Jellyfin or written into
a render until you approve it.** Decisions save straight to the
`.cleanmedia.json` sidecar, which is what the plugin reads — so approving
here *is* what makes Jellyfin skip a scene.

Findings marked **"needs your call"** are the `suggestive` category: a body
shown without explicit nudity, which is objectionable or not depending on
how the film frames it. Those are surfaced as questions, not verdicts.

Each finding carries a `reasoning` string with the offending word and its
surrounding dialogue, or the model's description of what it saw, so you can
usually judge without opening the film.

---

## Rendering a clean copy

```bash
scripts/render.sh "movies/Some Film (2010).mkv"
# or choose the output path
scripts/render.sh "movies/Some Film (2010).mkv" /mnt/clean/SomeFilm.mkv
```

Applies every **approved** finding in a single FFmpeg pass — blur, skip and
mute together — to `movies/cleaned/<name> (Clean).mkv`. Rejected and
undecided findings are ignored, and the script refuses to run if nothing is
approved rather than silently producing an unedited copy.

- **Mute-only renders stream-copy the video**, so the picture stays
  bit-for-bit identical and finishes in minutes.
- **Blur or skip forces a video re-encode** (NVENC when available).
- **Skips drop subtitle tracks**, because cutting shortens the timeline and
  copied subtitle cues would drift out of sync.
- Cuts should land on shot boundaries. A cut mid-shot is visible — the
  subject jumps while the framing stays identical, which reads as a
  corrupted file. Use `worker/shots.py` to find real boundaries.

---

## The Jellyfin plugin

`plugin/Jellyfin.Plugin.CleanMedia` — a C# plugin for Jellyfin 10.10+.

### What it does

It implements **`IMediaSegmentProvider`**. When Jellyfin needs segments for
a library item, the plugin takes that item's file path, calls the worker's
`GET /api/segments`, and returns approved **skip** segments. Clients with
segment support then skip those scenes during playback — no rendering, no
second copy of the file.

### What it deliberately does *not* do

**Muting and blurring are not possible during playback**, and the plugin
does not pretend otherwise:

- Jellyfin's client actions are only `None`, `Ask to Skip`, and `Skip`.
  There is no mute or blur action.
- There is no supported way for a plugin to inject FFmpeg filters into the
  transcoding pipeline; that extension point was removed for security, and
  direct play would bypass it anyway.

So the plugin filters mute and blur segments out and logs that they need a
rendered copy. Reporting them would be worse than useless — a client told to
*skip* a mute segment would jump over dialogue you only wanted silenced.

**Segment type.** `MediaSegmentType` is a fixed six-value enum (`Intro`,
`Outro`, `Preview`, `Recap`, `Commercial`, `Unknown`) and custom types are
not supported. Approved skips are reported as **`Commercial`**, the type
clients are most willing to skip automatically.

### Build and install

```bash
scripts/build-plugin.sh
# or build and install in one step
scripts/build-plugin.sh "//NAS/docker/jellyfin/config/plugins"
```

This writes `plugin/dist/CleanMedia/` containing the DLL and the
`meta.json` Jellyfin needs to list the plugin. Copy that folder into your
Jellyfin plugins directory and restart the server:

| Setup | Plugins directory |
|---|---|
| Docker / Synology container | the host folder mapped to `/config/plugins` |
| Linux | `/var/lib/jellyfin/plugins/` |
| Windows | `%ProgramData%\Jellyfin\Server\plugins\` |

**The build must match your server.** Jellyfin 10.11 ships `net9.0`
assemblies and puts `IMediaSegmentProvider` in
`MediaBrowser.Controller.MediaSegments`; 10.10 was `net8.0` with a
different namespace. The project targets **10.11** — a mismatched build is
silently ignored at startup. Change the `Jellyfin.Controller` version in
the csproj if you are on 10.10.

Then in **Dashboard → Plugins → Clean Media**:

1. Set **Worker URL** (e.g. `http://100.x.y.z:8765`)
2. Click **Test connection** — it reports worker version, engines and queue
3. Leave **Only use approved findings** on

Finally, per client, set the **Commercial** segment action to `Skip` or
`Ask to skip` under Playback settings. Client support as of 2026: Jellyfin
Web, Android, Android TV, Media Player and Findroid support segments;
Swiftfin does not yet; Kodi does not.

If the worker is unreachable the plugin returns no segments and logs a
warning — a sleeping GPU box never breaks playback or a library scan.

---

## Worker API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | version, GPU, engines, queue depth |
| `GET /api/capabilities` | each engine's categories, actions and options |
| `POST /api/jobs` | submit an analysis job |
| `GET /api/jobs` · `GET /api/jobs/{id}` | list / inspect jobs |
| `DELETE /api/jobs/{id}` | delete a job and its timeline |
| `GET /api/jobs/{id}/segments` | findings for a job |
| `PATCH /api/jobs/{id}/segments/{n}` | approve, reject, change action |
| `POST /api/jobs/{id}/render` | render a clean copy |
| `GET /api/segments?path=...` | **used by the plugin** — merged approved findings for a file |

---

## Timeline format

Every engine converts its output into this shape, which is what makes the
plugin engine-agnostic:

```json
{
  "schemaVersion": 1,
  "mediaFingerprint": "quickhash:...",
  "segments": [
    {
      "id": 1,
      "startMs": 1289400,
      "endMs": 1304250,
      "category": "sexual_activity",
      "confidence": 1.0,
      "engine": "vlm",
      "recommendedAction": "skip",
      "approved": true,
      "reasoning": "shots 412-418 (3 flagged): description of what was seen"
    }
  ]
}
```

Results are also written next to the media as `<name>.cleanmedia.json`, so
they survive independently of the worker database.

---

## Gotchas worth knowing

Each of these produced *plausible but wrong* output rather than an error.

**Telecined DVD rips lie about frame rate.** An NTSC MPEG-2 rip advertises
29.97fps while the decoder emits 23.976. Converting frames to timestamps
with the advertised rate puts every shot boundary at 0.8× its real time, and
an early render ended its video 25 minutes before its audio — while the file
duration looked exactly right. Always measure the real rate
(`worker/shots.py::true_fps`), and when verifying a cut, compare the last
video and audio timestamps rather than trusting duration.

**Discs carry several subtitle tracks and most are useless.** One disc had three
English tracks: two with 12 packets (forced signage) and one with 1,333 (the
dialogue). Selecting by language alone lands on a forced track and silently
finds nothing. Select by packet count.

**DVD subtitles are white glyphs with a black outline on transparency.**
Flatten them onto white and you get hollow outlines that OCR reads as
gibberish. Flatten onto **black**, then invert.

**FFmpeg's `-frame_pts` overflows at 35.8 minutes.** It writes microseconds
into a 32-bit field, so every timestamp past 2³¹ µs silently becomes zero.
Prefix the filter chain with `settb=1/1000` to work in milliseconds.

**Never leave FFmpeg's stderr on an undrained pipe.** It blocks forever once
the pipe buffer fills. Redirect it to a file.

---

## Development

```bash
scripts/test.sh     # 76 python tests, then the plugin build
```

Layout:

| Path | What it is |
|---|---|
| `worker/` | FastAPI app, engines, policy, rendering, review UI |
| `worker/policy.py` | what counts as a finding — pure logic, no model |
| `plugin/` | the Jellyfin plugin (C#) |
| `scripts/` | every routine command |
| `patches/` | local fixes to `pureframe` |
| `tests/` | 76 tests, most named for a bug they prevent recurring |

`patches/` holds fixes for `pureframe` 0.1.0b7, which ships an FFmpeg
deadlock, decodes the entire film once per shot, and crashes serializing
numpy types. `uv sync` overwrites them; `scripts/setup.sh` re-applies them.

---

## Roadmap

1. ~~**Worker** — API, queue, engines, timelines, rendering~~ ✅
2. ~~**Jellyfin plugin** — segment provider, settings, connection test~~ ✅
3. **Live mute/blur** — requires injecting JS into jellyfin-web (e.g. via
   [file-transformation](https://github.com/IAmParadox27/jellyfin-plugin-file-transformation))
   to drive `video.muted` and CSS filters, since no server-side hook exists
4. **Review UI** — approve findings in Jellyfin instead of via the API
5. **Policy** — one global action per category

See [plan/prds/clean-media-prd.md](plan/prds/clean-media-prd.md) for the PRD.

## Scope and licence

**This project ships no media and no filtering data.** It is a tool that
runs on your machine, against files you already have, and writes its
results next to them. There is no catalogue, no service, no server of ours
in the path, and nothing is uploaded anywhere. Detection is generic: it
looks at frames and subtitle text and knows nothing about any particular
title.

Two design choices follow from that, and they are deliberate:

* **Filtering happens during playback where possible.** The Jellyfin plugin
  reports approved ranges as skippable segments — the player skips them.
  Nothing is copied, nothing is redistributed, and the original file is
  what is being played.
* **Nothing is acted on without a person deciding.** Every finding starts
  undecided. The software proposes; the administrator disposes.

The renderer, which writes an edited second copy, exists for players that
cannot skip. That is a materially different act from skipping during
playback, and it is worth understanding the distinction in your own
jurisdiction before using it. In the United States the Family Movie Act of
2005 addresses making limited portions of a motion picture imperceptible
during a performance in a private household, and specifically contemplates
that no fixed copy of the altered version is created. Rendering creates
one. **This is not legal advice** — if that distinction matters to you, ask
a lawyer, and prefer the skip path.

Whatever route you take: use it on media you own, in your own home, for
yourself. Do not use it to distribute anything.

Licensed under the [MIT Licence](LICENSE).
