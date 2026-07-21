# Product Requirements Document — Clean Media for Jellyfin

> **Status: Phase 1 and Phase 2 built and validated on four films.**
> Last updated 2026-07-20. Sections marked ✅ are implemented and measured;
> ⏳ is in progress; ⬜ is not started.

---

## Vision

Give families a completely self-hosted, privacy-first way to identify,
review and manage objectionable content in media they already own.

All analysis runs on hardware the user owns. No media is uploaded to any
third party. The project aims to be the open-source standard for
AI-assisted media filtering.

---

## Goals

* ✅ Analyze local media using AI
* ✅ Offload AI workloads to a GPU workstation, keeping Jellyfin light
* ✅ Never modify original media
* ✅ Require administrator review before anything is acted on
* ✅ Remain completely self-hosted — no cloud APIs, telemetry or accounts
* ✅ Integrate into Jellyfin

### Non-goals (v1)

Streaming-service integration · cloud AI · per-user profiles · automatic
censorship without approval · mobile apps.

*Real-time playback filtering was a non-goal and remains largely
impossible — see [Jellyfin's limits](#what-jellyfin-can-and-cannot-do).*

---

## Architecture ✅

```
            +-----------------------+
            |      Jellyfin         |   Clean Media plugin (C#)
            |    (NAS / server)     |   IMediaSegmentProvider -> skips
            +-----------+-----------+
                        |
              HTTP over Tailscale/LAN
                        |
            +-----------v-----------+
            |   Clean Media Worker  |   Python + FastAPI
            |   (GPU workstation)   |   queue · store · review · render
            +-----------+-----------+
                        |
    +----------+--------+--------+-----------+
    |          |                 |           |
 subtitles   vobsub OCR        vlm        whisper
 (profanity) (profanity)  (visual, Ollama) (profanity)
```

Every detector is an adapter behind one interface, producing the same
timeline format — so engines can be added or replaced without touching the
plugin.

---

## Detection engines — measured, not claimed

| Engine | Detects | Verdict |
|---|---|---|
| **`subtitles`** ✅ | profanity | **Default.** Reads the film's own subtitle track; OCRs it if bitmap-based |
| **`vlm`** ✅ | nudity, sexual activity, kissing, suggestive | **Default for video.** Qwen3-VL via Ollama |
| `whisper` ✅ | profanity | Fallback only — no subtitle track at all |
| `pureframe` ⚠️ | nudity | **Not recommended.** Retained for comparison |

### Finding 1 — subtitles beat speech recognition decisively

| Test film | Subtitles / OCR | Whisper `medium.en` |
|---|---|---|
| A (126 min, text subs) | **9 of 9** | 4 of 9 |
| B (138 min, bitmap subs) | **12** | 7 |
| C (121 min, text subs) | **28** | not run |

Whisper's misses are not mistimings — the words are absent from its
transcript entirely. On film A, 3 of 5 misses were transcribed as pure
silence (voice-activity filtering discarding speech under music), and 2
were hallucinated or truncated sentences. On film B it missed the two
strongest lines in the script outright.

**Why:** a human already transcribed the dialogue and shipped it on the
disc. Reading it beats guessing at it.

### Finding 2 — a general VLM beats a purpose-built detector

On film A, PureFrame flagged **855 shots** — 44% of the runtime —
essentially all wrong; its two "explicit nudity" hits were a courtroom scene
and an explosion. The VLM flagged the one genuine scene and nothing else,
describing it accurately.

On another film PureFrame flagged 855 shots; the VLM flagged 6.

### Finding 3 — model size is a cliff, not a gradient

`qwen3-vl:2b-instruct` flagged a jellyfish, a starfish, a handwritten note
and glass vials as nudity at 0.95–0.97 confidence. No threshold rescues
that. **4B is the practical minimum.**

### Finding 4 — category wording matters more than model choice

Every large accuracy gain came from one-line prompt changes, each found by
looking at frames rather than trusting confidence scores:

| Problem observed | Fix |
|---|---|
| All misses were near-black frames | Brighten frames below mean luma 45 (CLAHE) |
| Shirtless men flagged as nudity | Nudity is gender-asymmetric; a man in trousers is not nudity |
| A woman's bare back missed | A woman whose bare back shows she is undressed *is* nudity |
| A sculpture, and a costumed character, flagged | Statues, cartoons and non-human figures are never nudity |
| A distant background figure flagged | If the body cannot be made out, answer false |
| An ordinary romantic kiss flagged | Only kissing "meant to be private" counts |

**Confidence scores are not calibrated.** The model reported 0.98 while
describing a piece of roadside signage. They indicate certainty, never
correctness.

### Finding 5 — a middle category was needed

A shirtless man is innocuous when swimming and not when the camera lingers
on him. That judgement depends on framing the model should not resolve
alone, so it reports **`suggestive`** — surfaced in review marked "needs
your call" rather than forced into a binary.

---

## Components

### 1. Clean Media Worker ✅ (Python / FastAPI)

Queues jobs · analyzes media · stores results · serves review UI · renders
clean copies. Runs on Windows, Linux and Docker.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | version, GPU, engines, queue depth |
| `GET /api/capabilities` | each engine's categories, actions, options |
| `POST /api/jobs` · `GET /api/jobs[/{id}]` · `DELETE` | job lifecycle |
| `GET /api/jobs/{id}/segments` · `PATCH .../{n}` | findings, approval |
| `POST /api/jobs/{id}/render` | render a clean copy |
| `GET /api/segments?path=` | **plugin** — merged approved findings |
| `GET /api/review?path=` | **administrator review UI** |
| `GET /api/thumbnail` · `GET /api/clip` | review stills and playable clips |
| `PATCH /api/segments/{id}?path=` | approve / reject a finding |

**Batch mode** (`python -m worker.batch`) analyzes files, folders or globs,
runs cheap engines before expensive ones, caches per-engine results, and
never re-ingests a rendered `(Clean)` copy.

### 2. Review UI ✅

Serves every finding with a thumbnail and **a playable clip padded 15
seconds either side**, seeking to where the flagged part begins. Approve
and reject write straight to the `.cleanmedia.json` sidecar.

That sidecar is what the plugin reads, and the plugin requests
`approvedOnly=true` — so **approving a finding here is what makes Jellyfin
skip it.** Nothing acts on a finding until approved.

### 3. Jellyfin Plugin ✅ (C#, Jellyfin 10.10+)

Implements `IMediaSegmentProvider`. Resolves a library item's path, calls
the worker, returns approved **skip** segments. Settings page with a
connection test. An unreachable worker returns nothing and logs a warning —
a sleeping GPU box never breaks playback or a library scan.

---

## What Jellyfin can and cannot do

Researched against the shipping API, not assumed:

| Action | Native support | Status |
|---|---|---|
| **Skip** a range | Media Segments API (10.10+) | ✅ works |
| **Mute** a range | none — no client action exists | ❌ needs a rendered copy |
| **Blur** a range | none — no FFmpeg injection point | ❌ needs a rendered copy |

`MediaSegmentType` is a fixed six-value enum with no "objectionable" member
and no custom types, so approved skips are reported as **`Commercial`** —
the type clients most willingly auto-skip.

The plugin deliberately **filters mute and blur segments out**. Reporting
them would be worse than useless: a client told to *skip* a mute segment
would jump over dialogue the administrator only wanted silenced.

**Client support (2026):** Web, Android, Android TV, Media Player and
Findroid honour segments; Swiftfin does not yet; Kodi does not.

---

## Rendering ✅

One FFmpeg pass applies an approved timeline — blur, skip and mute
together — to a **new file**. Originals are never touched.

* Mute-only renders **stream-copy the video** (bit-for-bit identical, minutes)
* Blur or skip forces a re-encode (NVENC where available)
* Skips **drop subtitle tracks**, since cutting desynchronises copied cues
* Cuts must land on shot boundaries — a mid-shot cut reads as file corruption

---

## Standard timeline format ✅

```json
{
  "schemaVersion": 1,
  "mediaFingerprint": "quickhash:...",
  "segments": [{
    "id": 1, "startMs": 1289400, "endMs": 1304250,
    "category": "sexual_activity", "confidence": 1.0,
    "engine": "vlm", "recommendedAction": "skip", "approved": true,
    "reasoning": "shots 412-418 (3 flagged): description of what was seen"
  }]
}
```

Written next to the media as `<name>.cleanmedia.json`, so results survive
independently of the worker database.

### Categories

* **v1 ✅** nudity · sexual activity · intense kissing · suggestive · profanity
* **v2 ⬜** violence · gore · drug use · alcohol · smoking
* **Future ⬜** scary scenes · custom categories

Profanity tiers, per administrator: **strong** (always) includes hell and
damn · **blasphemy** (opt-in) · **mild** (opt-in) is the kid register —
stupid, idiot, poop.

---

## Performance — measured

| Pass | Throughput |
|---|---|
| Subtitles (profanity) | **~1 minute per film** |
| VobSub OCR (first time only, then cached) | ~15 minutes per film |
| Visual (VLM) | **0.20 frames/sec — ~4 hours per film** |

The visual pass is **entirely CPU-bound**: Ollama reports `total_vram="0 B"`
on the current machine despite a working RTX 3050 that PyTorch detects
fine. **Resolving that is the single highest-value optimisation** — a
working GPU should give 5–15×, putting a film at 20–45 minutes.

**Recommended workflow at library scale:** run the audio pass across
everything first (an afternoon for a large library), then choose which
titles justify the visual pass.

---

## Bugs worth remembering

Every one produced **plausible but wrong output rather than an error**.

1. **Telecine frame rates.** Every DVD rip here advertises 29.97fps and
   decodes at 23.976. Converting frames to timestamps with the wrong rate
   put shot boundaries at 0.8× real time and desynced a render's video 25
   minutes from its audio — while the file duration looked exactly right.
2. **Partial shot timelines.** PySceneDetect indexes frames on the
   container timebase; dividing by the decode rate pushed one film's last
   480 shots past EOF. Those frame grabs returned nothing, were skipped, and
   the run reported success — 21% of the film unanalyzed. *Now guarded: a
   shot list covering under 95% of the duration raises.*
3. **Forced subtitle tracks.** One disc had three English tracks: two with
   12 packets (signage) and one with 1,333 (dialogue). Selecting by language
   alone finds nothing. *Select by packet count.*
4. **Subtitle compositing.** DVD subtitles are white glyphs with black
   outlines on transparency; flattening onto white leaves hollow outlines
   OCR reads as gibberish. *Flatten onto black, then invert.*
5. **`-frame_pts` overflows at 35.8 minutes.** Microseconds in a 32-bit
   field; every later timestamp became zero. *Use `settb=1/1000`.*
6. **Undrained FFmpeg stderr deadlocks.** Redirect it to a file.
7. **Thinking models return empty content.** The bare `qwen3-vl` tags
   reason for hundreds of tokens before answering. *Use `-instruct`.*

**The pattern:** verify the invariant, not the exit code. Duration matching
did not prove a render was synced; a successful ffmpeg run did not prove
frames were extracted.

---

## Phase roadmap

### Phase 1 — Worker ✅
FastAPI · job queue · four engines · progress · timelines · rendering ·
batch mode · Docker · 58 tests

### Phase 2 — Jellyfin plugin ✅
`IMediaSegmentProvider` · settings page · connection test · review UI with
clip playback

### Phase 3 — Live mute and blur ⬜
No server-side hook exists. The viable route is injecting JavaScript into
jellyfin-web (e.g. via `jellyfin-plugin-file-transformation`) to drive
`video.muted` and CSS filters. Covers web, Android and Media Player; Apple
TV would still need rendered copies.

### Phase 4 — Scale and policy ⬜
GPU inference · review UI inside Jellyfin · one global action per category ·
library-wide scheduling

---

## Success criteria

| Criterion | Status |
|---|---|
| Install the worker | ✅ |
| Connect Jellyfin to the worker | ✅ plugin + connection test |
| Analyze a movie | ✅ four films |
| Review AI-detected segments | ✅ thumbnails + clips + approve/reject |
| Save approved results | ✅ sidecar, read by the plugin |
| Preserve the original media | ✅ always a new file |
| Run entirely offline | ✅ no cloud calls |
| Support new engines without architectural change | ✅ four engines, one interface |

**Validated end to end:** a test film has a rendered clean copy with an
approved scene cut on real shot boundaries and 18 profanity words muted at
word level — verified for A/V sync and silence in every mute window.
