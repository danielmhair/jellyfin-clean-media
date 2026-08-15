# Spec — In-Jellyfin queue manager + folded-in settings

Status: **completed** (2026-08-14, verified live in Jellyfin) · Date: 2026-08-14 ·
Area: Jellyfin plugin dashboard + worker job queue.

## Completion notes

Shipped and confirmed working in a running Jellyfin dashboard.

- **Backend (persisted-order queue).** FIFO → `queuePosition`-ordered selection
  on a condition variable ([worker/queue.py](../../worker/queue.py)); selection
  is schedule-aware so a reorder made while a job waits out its window takes
  effect (the worker never commits to a job while waiting). Added `reorder`,
  `requeue`, `set_paused`/`is_paused`; `queuePosition` on the `Job` model;
  recovery assigns positions by `createdAt` to any job predating the field.
- **New worker endpoints** ([worker/main.py](../../worker/main.py)):
  `POST /api/jobs/reorder`, `POST /api/jobs/{id}/requeue` (404/400), and
  `POST /api/jobs/pause`; `paused` added to `GET /api/health`.
- **Tests** — `tests/test_queue.py` (real `JobQueue`, asserts on run order) and
  `tests/test_jobs_api.py` (endpoints); `test_recovery.py`/`test_schedule.py`
  stay green. Full non-e2e suite: 185 passing.
- **Plugin C#** — job DTO gains `queuePosition`/`createdAt`, health gains
  `paused`; `ReorderJobsAsync`/`RequeueJobAsync`/`SetPausedAsync` and the
  `Jobs/Reorder`, `Jobs/{id}/Requeue`, `Jobs/Pause` proxies.
- **Dashboard page** — [reviewPage.html](../../plugin/Jellyfin.Plugin.CleanMedia/Configuration/reviewPage.html)
  rewritten as the tabbed Settings + Queue page per the final mockup.
- **Config page retired** — `configPage.html` deleted; the tabbed page is now the
  single plugin page (main-menu entry + config destination).

Reference prototypes (design source of truth, throwaway — capture to a branch,
don't keep on `main`):
[plan/prototypes/queue_manager_final.html](../prototypes/queue_manager_final.html)
(the target look — Settings + Queue in one page) and
[plan/prototypes/queue_manager_prototype.html](../prototypes/queue_manager_prototype.html)
(interactive, validates the reorder/requeue/state logic).
Supersedes the current library-grid page in
[plugin/…/Configuration/reviewPage.html](../../plugin/Jellyfin.Plugin.CleanMedia/Configuration/reviewPage.html).

## Problem Statement

The administrator has no single place to manage analysis/render work. Today they
can only *add* jobs (per-film "Analyze") and *cancel* them (per-job or "Cancel
all"), and the order is fixed to submission order — a job can't be moved ahead
without cancelling and re-adding, because the queue is an in-memory FIFO. There's
no list view of everything queued, no way to reorder, delete a single item, or
retry a failed one. Separately, the plugin's **settings** live on a different,
utilitarian config page (worker URL, timeout, playback flags, and a clunky 7×2
grid of raw time inputs for the analysis schedule) that the admin has to navigate
away to reach. The two concerns — "what is the worker doing / what should it do
next" and "how is the worker configured" — are split across two plain pages that
read as hobbyist and fight the person using them.

## Solution

Replace the plugin dashboard page with a single, Jellyfin-native **Clean Media**
page with two tabs — **Settings** (default) and **Queue** — so configuration and
work management live in one place, no separate settings page or button.

- **Queue tab** — a colour-coded, type-aware manager. A left column holds a type
  filter ("Show") and an **Add to queue** picker that drills library collections
  → films; a right column holds the live queue in three groups: **Running now**
  (with progress + ETA), **Up next** (drag-handle reorder, per-row delete), and
  **Recent** (finished / failed, with **Requeue** on failures and **Review** on
  completed). On narrow screens the two columns stack.
- **Settings tab** — the current config page, redesigned: a **Worker connection**
  card with a live status pill, a **Playback** card with real toggles, and an
  **Analysis schedule** with a **visual weekly editor** (per-day on/off + start/
  end pickers in **12-hour AM/PM, 15-minute** steps, a 24-hour band that wraps
  past midnight, and "Copy Monday to every day"), gated by a simple **Always on
  / Scheduled hours** mode, with one sticky Save/Discard bar.

Underneath, the FIFO job queue becomes a **persisted-order queue** so reorder is
possible, and gains **requeue** for failed/cancelled jobs. Nothing changes about
the sidecar/timeline schema, the `approvedOnly` contract the segment provider
reads, or the render path.

## User Stories

### Queue — viewing & structure
1. As an administrator, I want to see every analysis and render job in one list, so that I know what the worker is doing and what's waiting.
2. As an administrator, I want the queue split into Running now, Up next, and Recent, so that I can tell at a glance what's active, what's pending, and what's finished.
3. As an administrator, I want the single running job shown with a progress bar, stage text, and an ETA, so that I know how far along it is.
4. As an administrator, I want each job's pass type shown and colour-coded (profanity-subtitles, profanity-whisper, visual, render), so that I can scan the queue by type.
5. As an administrator, I want the running job to be visibly non-reorderable, so that I understand the current pass won't be interrupted.
6. As an administrator, I want the list to update live while something is running, so that I don't have to refresh.

### Queue — reordering
7. As an administrator, I want to reorder queued jobs by dragging a handle, so that I can decide what runs next.
8. As an administrator, I want reordering to affect only queued jobs and never preempt the running one, so that a long pass on the single GPU isn't interrupted mid-run.
9. As an administrator, I want the new order to persist, so that it survives a page reload and a worker restart.
10. As an administrator, I want each queued row to show its position, so that I can see where it sits in line.

### Queue — add / delete / requeue
11. As an administrator, I want to add jobs without leaving this page, so that adding is part of managing the queue.
12. As an administrator, I want to choose a pass type first (profanity-subtitles / -whisper / visual / render clean copy), so that I control what pass a film gets.
13. As an administrator, I want to browse my libraries as collections and drill into one to see its films, so that I add work the same way I navigate my library.
14. As an administrator, I want to click a film to queue it with the chosen pass type, so that adding is one click once I've drilled in.
15. As an administrator, I want a back control from a collection's films to the collection list, so that I can move between collections.
16. As an administrator, I want a search field to find a film by name, so that I don't have to drill in for a title I already know.
17. As an administrator, I want to delete a single queued item, so that I can drop something I no longer want analysed.
18. As an administrator, I want to cancel a running job from its row, so that I can stop a pass I started by mistake.
19. As an administrator, I want a "Cancel all" action, so that I can clear the whole queue at once.
20. As an administrator, I want to requeue a failed or cancelled job, so that I can retry it after fixing the cause (e.g. a missing subtitle track).
21. As an administrator, I want a requeued job to go to the back of the queue by default and then be draggable to the front, so that retries are fair but promotable.
22. As an administrator, I want to remove a finished/failed item from the Recent list, so that I can tidy the history.
23. As an administrator, I want to render a clean copy by choosing the "Render clean copy" pass and a film, so that rendering uses the same add flow as analysis.
24. As an administrator, I want to open the worker review page for a completed film from its Recent row, so that I can review its findings.
25. As an administrator, I want to pause and resume the whole queue, so that I can stop new jobs from starting without cancelling what's queued.

### Queue — type filter
26. As an administrator, I want a "Show" filter with a chip per pass type, so that I can focus on one kind of work.
27. As an administrator, I want the filter to be multi-select and show a count per type, so that I can combine types and see how many of each there are.
28. As an administrator, I want the filter to scope every group (Running / Up next / Recent) at once, so that the whole queue reflects my focus.
29. As an administrator, I want a "Show all" reset, so that I can return to the full queue.

### Layout
30. As an administrator on a wide screen, I want the controls (Show + Add to queue) on the left and the queue lists on the right, so that the picker has room and the queue is the focus.
31. As an administrator, I want the Add to queue picker to fill the left column's height, so that I can browse collections/films with room to scroll.
32. As an administrator on a narrow screen, I want the layout to stack into one column, so that it stays usable in a small window or the mobile dashboard.

### Settings — one page
33. As an administrator, I want settings and the queue in the same page as tabs, so that I don't navigate to a separate settings page.
34. As an administrator, I want Settings to be the default tab, so that first-time configuration is front and centre.

### Settings — worker connection
35. As an administrator, I want a live connection status (connected / version / GPU / queue state), so that I can tell the worker is reachable without running a separate test.
36. As an administrator, I want to edit the Worker URL and timeout, so that I can point Jellyfin at my worker.
37. As an administrator, I want a "Test connection" action, so that I can confirm reachability on demand.

### Settings — playback
38. As an administrator, I want a toggle for "Skip approved scenes during playback", so that I control whether approved skips surface as Commercial segments.
39. As an administrator, I want a toggle for "Only use approved findings", so that unreviewed AI guesses are not acted on.
40. As an administrator, I want the note that Jellyfin can only skip live (mutes/blurs need a rendered copy), so that my expectations are correct.

### Settings — analysis schedule
41. As an administrator, I want to choose between "Always on" and "Scheduled hours", so that I decide simply whether analysis is time-restricted.
42. As an administrator, when I pick "Always on", I want the editor to disappear and a note to say analysis runs whenever queued, so that the state is unambiguous.
43. As an administrator, I want a weekly editor with a row per day, so that I can set different hours per day.
44. As an administrator, I want per-day on/off, so that I can exclude some days (e.g. weekdays).
45. As an administrator, I want to set each day's start and end times, so that I control the allowed window.
46. As an administrator, I want the time pickers in 12-hour AM/PM format, so that they read naturally.
47. As an administrator, I want the time options in 15-minute increments, so that I can place windows precisely.
48. As an administrator, I want a visual 24-hour track per day showing the allowed window, so that I can see the schedule, not just read numbers.
49. As an administrator, I want a window whose end is earlier than its start to render as wrapping past midnight, so that overnight windows are obvious.
50. As an administrator, I want a "Copy Monday to every day" action, so that I don't set seven days by hand.
51. As an administrator, I want one Save/Discard bar for all settings, so that I save connection, playback and schedule together.

### System / contracts
52. As the segment provider, I want the sidecar and `approvedOnly` read unchanged, so that approved skips still surface as Commercial segments with no change to playback behaviour.
53. As the worker, I want unfinished jobs to resume in their intended order after a restart, so that a reboot doesn't lose the reorder.

## Implementation Decisions

### The page this replaces
The target is the plugin's **dashboard page** (`reviewPage.html`, `EnableInMainMenu`),
not the worker's `/api/review` Studio page. The separate plugin **config page**
(worker URL / timeout / playback / schedule) is folded into this page's Settings
tab and retired as a destination; per-film findings review still happens on the
worker's `/api/review` page, which the Queue tab links to for completed films
(the existing `window.open(workerUrl + '/api/review?path=…')` behaviour is kept).

### Backend: FIFO → persisted-order queue (the one structural change)
The worker thread currently blocks on an in-memory `queue.Queue` and runs jobs
in put-order, which cannot be reordered. Replace the ordering with a persisted
`queuePosition` on the job; the worker always runs the lowest-position *runnable*
job. The queue "kind" (analyze / render / render_media) is already derivable from
a persisted job (the existing `_kind_for` classification, unit-tested today), so
the in-memory `(kind, job_id)` tuple is dropped.

- **Model:** add an optional `queuePosition` to the job model (lower = sooner;
  unset sorts last / is derived on recovery). Additive — old rows load fine.
- **Selection:** the worker loop waits on a condition variable (signalled on
  submit/reorder/requeue) and, when woken, picks the runnable job (`queued` or
  `rendering`, not cancelled) with the smallest `(queuePosition, createdAt)`. The
  condition wait uses the existing `poll_s` as its timeout, so schedule-gated jobs
  re-evaluate and tests stay deterministic. Analysis jobs still self-gate via the
  existing schedule wait; the running job is never preempted.
- **Submit:** assign the next position, save, signal. Dedup/validation unchanged.
- **Recovery:** keep the existing status resets (running→queued etc.); assign
  positions by `createdAt` to any recovered job lacking one, preserving
  submission order.
- **reorder(ids):** reassign `queuePosition = 0..n` in the given order, but only
  for jobs currently queued/rendering (ignore running/terminal/unknown ids).
- **requeue(id):** for a failed/cancelled job, reset to queued (progress 0, clear
  error/stage), assign a fresh end-of-queue position, discard from the cancelled
  set, signal. Raise for missing/not-requeueable.

The interactive prototype encodes the model precisely (trimmed to the decisions):

```
isPending(j)  = status ∈ {queued, rendering}
pending()     = jobs.filter(isPending).sort by queuePosition        // runnable, in order
reorder(ids)  = for each id in ids, if isPending: queuePosition = p++  // 0..n
requeue(id)   = if status ∈ {failed, cancelled}:                     // else refuse
                  status=queued; progress=0; error=null; queuePosition = alloc() // back of queue
// worker: run the lowest-position pending job; when it finishes, promote the next. Never preempt running.
```

### Backend: new endpoints
Two additive worker endpoints alongside the existing job routes:
- `POST /api/jobs/reorder` — body `{ "ids": [...] }` → returns the updated job
  list (UI re-renders from truth).
- `POST /api/jobs/{id}/requeue` → returns the job (404 unknown, 400 not
  requeueable).
`GET /api/jobs`, `DELETE /api/jobs/{id}` (cancel-or-forget), `POST /api/jobs`
(add), `POST /api/jobs/cancel-all`, `POST /api/render`, `POST /api/status`,
`GET /api/health`, and the schedule endpoints are reused unchanged.

### Backend: pause/resume the queue
Add a global paused flag on the queue: while paused the worker holds *starting*
the next job (it does not preempt a running one), analogous to the schedule gate.
Exposed via a small worker endpoint (e.g. `POST /api/jobs/pause` `{paused}`) and
reflected in `GET /api/health`/status so the UI shows the paused state.

### Plugin proxy (C#)
- Job DTO gains `queuePosition` and `createdAt` (for order + display).
- New proxied controller actions: `POST CleanMedia/Jobs/Reorder`,
  `POST CleanMedia/Jobs/{id}/Requeue`, and a pause proxy. Existing `Jobs` (list),
  `Jobs/{id}` (delete), `Analyze`, `Render`, `Status`, `Findings`,
  `Jobs/CancelAll`, `Schedule`, `Config`, `TestConnection` are reused.
- The **Render clean copy** pass type routes through the existing Render proxy
  (by item id → path), not the analysis submit; the other pass types use the
  existing Analyze proxy.

### Plugin dashboard UI (replaces the page)
Port the final-look mockup into `reviewPage.html` as a single self-contained
server-rendered document (consistent with the current page), using Jellyfin's own
chrome (it renders inside the dashboard). Structure:
- **Tabs:** Settings (default) and Queue.
- **Queue tab** — wide screens: left column = "Show" type filter + "Add to
  queue"; right column = Running now / Up next / Recent. Narrow: stacked.
  - **Type filter:** multi-select chips per pass type with counts; scopes all
    three groups; "Show all" resets.
  - **Add to queue:** pass-type segmented control (incl. Render clean copy) that
    stays selected across the flow; collections list → drill into films → click
    to queue; a back control; a film search field. Films/collections come from
    Jellyfin's own item APIs (real posters), replacing the mockup's stand-ins.
  - **Rows:** colour-coded stripe + glyph + type tag; Running shows progress/ETA
    (reuse the existing client-side ETA) + Cancel; Up next shows a drag handle +
    position + Delete; Recent shows a status badge + Requeue (failed/cancelled) /
    Review (completed, opens the worker review page) + Remove.
  - **Controls:** Cancel all, Pause/Resume queue. Live polling while active
    (reuse the existing poll pattern).
- **Settings tab** — three cards: Worker connection (live status pill from
  `TestConnection`/health + URL/timeout + Test), Playback (toggles mapping to the
  existing plugin config: skip-approved and only-approved, plus the skip-only
  note), and Analysis schedule. One sticky Save/Discard bar; Save writes the
  plugin config and the worker schedule (existing endpoints).

### Settings: schedule editor (encodes the decisions)
Two modes: **Always on** (no restriction; editor hidden; note shown) and
**Scheduled hours** (editor shown). Per day: on/off + start/end selects in
12-hour AM/PM at **15-minute** granularity, plus a 24-hour visual band. Band math
from the prototype (minutes 0–1440):

```
band(s, e):
  if s == e: whole day
  if s <  e: [ s .. e ]
  else:      [ s .. 1440 ] and [ 0 .. e ]     // wraps past midnight
```

"Copy Monday to every day" copies the first row's start/end to all days. The
schedule maps onto the worker's existing schedule shape (per-day enable + start/
end minutes) — no schedule-schema change; only the *editor* is new.

### No change to
The sidecar/timeline schema, the `MANUAL_ENGINE` merge semantics, the
`approvedOnly` read the segment provider uses, the render engines, and the
worker's schedule data shape. The reorder/requeue/pause additions and the two new
job endpoints are the only backend surface changes.

## Testing Decisions

A good test asserts **observable behaviour**, not internals: for the queue, drive
the real `JobQueue` and assert on run order and persisted job state; for the
endpoints, drive the FastAPI app and assert on responses + resulting job rows —
never on private fields or DOM structure.

- **Primary seam — the `JobQueue` (unit).** This is the highest, most
  deterministic seam and where the one structural change lives. Add
  `tests/test_queue.py` mirroring `tests/test_recovery.py`'s harness (the
  injectable `JobQueue(store, allowed_fn, now_fn, sleep_fn, poll_s)` with a
  `_RecordingEngine` that records run order, and the `_wait` poller):
  - reorder changes the actual run order (queue several jobs held outside the
    schedule window via `allowed_fn`, reorder, open the window, assert the engine
    ran them in the new order);
  - submit assigns increasing `queuePosition`;
  - requeue resets a failed job to queued (error cleared) and it runs; requeue is
    refused for a running/queued job;
  - reorder ignores running/terminal/unknown ids without disturbing them;
  - pause holds the next start and resume releases it;
  - recovery still runs recovered jobs in submission order (keep
    `test_recovery.py` green).
- **Secondary seam — worker API (function/endpoint).** In the style of
  `tests/test_review.py`/`test_status.py`, cover `POST /api/jobs/reorder`,
  `POST /api/jobs/{id}/requeue`, and pause: correct response shape, persistence,
  and the 404/400 error cases. Prefer asserting through these endpoints over
  adding new seams — they are thin over the `JobQueue` methods above.
- **Plugin UI — manual.** The plugin dashboard page has no automated harness (the
  `tests/e2e` Playwright suite targets the *worker* review page, not the plugin),
  so the tabbed page, drag reorder, collection drill-in, type filter, and the
  schedule editor are verified manually after `scripts/build-plugin.sh` + install,
  walking the page in a running Jellyfin — consistent with the project's
  "verify the invariant in a real browser" discipline. The design/interaction was
  pre-validated in the prototypes.

## Out of Scope

- Any change to the worker's `/api/review` Studio page or its features (the
  in-plugin page links to it for per-film review, unchanged).
- The sidecar/timeline schema, `approvedOnly` contract, render engines, and the
  worker's schedule data shape.
- A backend film search index — film search in Add-to-queue uses Jellyfin's own
  item query (client-side over the browsed items), not a new worker endpoint.
- Drag-to-edit of the schedule band (times are edited via the AM/PM pickers; the
  band is a read-only visualisation).
- Bulk add of a whole collection in one click — the picker drills in and adds
  individual films (matches the agreed flow); collection-level bulk add is a
  possible later follow-up.

## Further Notes

- The prototypes are the design source of truth for layout, the colour-per-type
  system, drag-handle reorder feel, the collection drill-in, and the schedule
  editor. They are throwaway: capture them to a branch when this lands and remove
  them from `main` (same disposition as the Studio prototype).
- Open product calls already defaulted (change if wanted): requeue goes to the
  **back** of the queue (then draggable); the Add-to-queue picker is **prominent**
  (fills the left column) rather than collapsed behind a button.
- **Tracker note:** no `gh` CLI / triage-label vocabulary is configured in this
  environment, so this spec is filed in `plan/prds/` (the project's spec home)
  rather than as a `ready-for-agent` GitHub issue. To publish it as an issue,
  install/configure `gh` and re-run the publish step.
