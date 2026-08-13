# Handoff — reliability, telecine & review session (2026-08-12)

Branch `main`. Read [PROGRESS.md](../../PROGRESS.md),
[docs/FEATURES.md](../../docs/FEATURES.md), and [CLAUDE.md](../../CLAUDE.md)
first — this doc only covers what those don't. Keep it generic (no real film
names) like every committed file.

## The one thing that matters most

**This session's commits are code-complete but NOT live in the running worker.**
A long-lived Python process loaded the old code at its last restart; editing
files on disk does nothing until it restarts.

**Action — restart the worker** (elevated PowerShell, repo root):
```powershell
.\scripts\install-service.ps1 -MediaRoots "\\Nas\nas-8tb-hdd\Movies" -AtLogon
```
Use the full re-register (not `-Restart`) so it also picks up the hidden-window
launcher. On boot, the queue's recovery re-enqueues every unfinished job in
submission order and the in-flight VLM pass resumes from its 25-sample
checkpoint. This is the user's call to run; it briefly interrupts the current
pass (safe).

The session's commits are unpushed and the user hasn't decided whether to push.
See `git log --oneline` from `162f140` onward; each message is the record of
what/why — don't re-summarise them here.

## Follow-ups after the restart (verification the session couldn't finish)

1. **Re-queue the two telecine failures.** Two DVD rips are in the store with
   status `failed` — their `vlm` job hit the shot-coverage guard (one at 93 %,
   one at 95 %). Recovery skips terminal jobs, so they won't retry. Find them
   with `GET /api/jobs` (status `failed`, engine `vlm`) and re-submit
   (`POST /api/jobs {mediaPath, engine}`); also re-submit the `whisper` job for
   the one whose audio pass hit a transient NAS error. Watch the shot-detection
   phase land at **full coverage** — that's the live proof of the telecine
   rescale fix, which is otherwise only validated by recorded span/duration
   numbers + unit tests.
2. **Visually confirm the review preview** — the cut-marker strip and "Preview
   skip" button on `/api/review`. Renders + tests pass but it was never seen in a
   browser. Use any already-completed film's review page.
3. Timing precision on the review card is fixed and was verified end-to-end;
   nothing left there.

## Hard-won facts (don't rediscover)

- **GPU throughput is the ceiling, and it's accepted as a grind.** 4 GB card →
  `ollama ps` shows the 4B VLM at ~48 %/52 % CPU/GPU → ~12 s/sample → ~24 h per
  film. The CPU half is the critical path; closing desktop apps freed VRAM but
  did NOT speed it up (ollama caps the GPU split on 4 GB). Real fix = ≥6 GB card
  or offload Ollama (`host` option / `OLLAMA_HOST_URL`). User chose "leave it
  grinding." Don't relitigate.
- **NEVER run the full pytest suite against a live worker carelessly.** Importing
  `worker.main` builds a live `Store`/`JobQueue` on the real DB; this used to
  reset the running job. Now `tests/conftest.py` points `CLEANMEDIA_DB` at a
  throwaway DB — suite is safe (159 green). Still pass `--basetemp=<writable>`
  (the sandbox blocks pytest's default temp).
- **Logs**: `data\logs\worker.log` (structured, rotating) and
  `%LOCALAPPDATA%\CleanMedia\worker.log` (raw stdout). Default level DEBUG. Watch
  live: `Get-Content data\logs\worker.log -Wait -Tail 50`. The worker runs as a
  background task, so its output never shows in the launching terminal.
- **Timing edits save correctly** (server + client preserve exact ms — verified
  via API round-trip and a browser). The earlier "not preserving" report was the
  card rounding to tenths, now fixed.
- **The waveform recenters on re-open** (window = finding ±15 s), so handles look
  centred even though values are correct. User was offered a change to *not*
  recenter when just re-checking an edit — **awaiting their decision** (the one
  genuinely open item from this session).
- A stray `data/schedule.json` from a demo was deleted (schedule back to
  unrestricted); Oculus `OVRService` was set to `Manual` (won't auto-start).
- **Timing editing lives on the worker review page** (`review.py` /
  `/api/review`); the plugin's `reviewPage.html` only links to it.

## Open threads (detail in PROGRESS.md, don't duplicate)

- **Slice 8 (polish) is ← NEXT**: grid sort by pending, overlapping-findings
  display, subtitle→whisper fallback, server-side ETA.
- **CrisperWhisper** install decision still pending (see Open decisions).

## Suggested skills

- **`run`** — launch/verify the worker + app after the restart, rather than
  trusting exit codes (the project's recurring failure mode is "reports success,
  produces garbage" — verify the invariant).
- **`playwright-cli`** — visually confirm the review-page preview on a completed
  film's `/api/review`. It was used this session to debug the timing editor;
  `.playwright-cli/` is gitignored.
- **`code-review`** — before any push, review this session's diff on `main`.
