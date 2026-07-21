# PRD — In-Jellyfin Review UI

> **Status: all three slices built; 102 tests passing; not yet loaded into a
> running Jellyfin.**
> Created 2026-07-20, updated 2026-07-21. ✅ implemented and verified ·
> ⏳ in progress · ⬜ not started.
>
> Extends [clean-media-prd.md](clean-media-prd.md). That document covers
> analysis, rendering and playback skipping; this one covers the review
> loop that gates all of it.

---

## Problem Statement

Nothing Clean Media detects has any effect until an administrator approves
it. The plugin requests `approvedOnly=true`, so an unreviewed film skips
nothing during playback — review is not a nice-to-have step, it is the
step that makes the product do anything at all.

Yet review is the one part that lives outside Jellyfin. Today an
administrator must:

1. know the film's absolute path on the worker's filesystem,
2. run `scripts/review.sh "<path>"` from a shell on the worker,
3. work through a single-film HTML page served from the worker's own port.

There is no list of films, no indication of which have been reviewed, no
way to see what is left to do, and no way to start an analysis from the
UI. Reaching the review page at all requires the worker to be reachable
from the administrator's *browser*, which breaks the moment the worker is
only reachable from the Jellyfin *server* — the Tailscale-only setup the
project otherwise recommends.

The result: the product's gating step is its least accessible one.

## Solution

Move review into the Jellyfin dashboard, as a plugin page alongside the
existing settings page — the same pattern Intro Skipper and Segment Editor
use, and one Jellyfin fully supports.

From the dashboard an administrator can pick a library, see every video
with its review state at a glance, click into one, and work through its
findings against a real video player: watch the moment, adjust the
timings, approve or reject, add a finding the AI missed, and queue up
films that have not been analyzed yet.

All worker traffic is proxied through the plugin's own authenticated
endpoints, so the worker never needs to be reachable from the browser —
only from the Jellyfin server, which is the machine that must reach it
during playback anyway.

## User Stories

**Finding work to do**

1. As an administrator, I want a Clean Media Review entry in the dashboard sidebar, so that I can reach review without leaving Jellyfin.
2. As an administrator, I want to pick which library to review, so that I can work through Kids before Marvel.
3. As an administrator, I want to see every video in a library as a poster grid, so that I recognise films visually rather than by path.
4. As an administrator, I want each video to show its review state, so that I can tell at a glance what needs my attention.
5. As an administrator, I want to see how many findings are still awaiting a decision, so that I know how much work a film represents before I open it.
6. As an administrator, I want to see which films have never been analyzed, so that I know what to queue.
7. As an administrator, I want to see which films are queued or currently analyzing, with progress, so that I do not queue them twice.
8. As an administrator, I want to see when an analysis failed, so that I can retry it rather than assume the film is clean.
9. As an administrator, I want a clear message when the worker is unreachable, so that I do not mistake a connection problem for an empty library.
10. As an administrator, I want to filter the grid to films needing review, so that I can work a backlog without scrolling past finished ones.
11. As an administrator, I want to sort by pending count, so that I can clear the quickest films first.

**Reviewing findings**

12. As an administrator, I want to click a film and see all of its findings, so that I can review them one after another.
13. As an administrator, I want to see each finding's category, engine, confidence and reasoning, so that I can judge whether the AI was right.
14. As an administrator, I want to see visual findings and profanity findings in one list, so that I review a film once rather than per engine.
15. As an administrator, I want to watch the actual moment in the film, so that I can decide based on the scene rather than a timestamp.
16. As an administrator, I want to scrub the whole film in the player, so that I can check context before and after a finding.
17. As an administrator, I want to jump the player to a finding's start with one click, so that I do not hunt for it on the scrub bar.
18. As an administrator, I want to approve a finding, so that Jellyfin skips it during playback.
19. As an administrator, I want to reject a finding, so that a false positive stops appearing and is never acted on.
20. As an administrator, I want my decision saved immediately, so that I do not lose work if I navigate away.
21. As an administrator, I want to see which decision I previously made, so that returning to a film shows me my own history.
22. As an administrator, I want to jump to the next undecided finding, so that I can work through a film without returning to a list.
23. As an administrator, I want a running count of decided vs remaining, so that I can see progress within a film.

**Editing findings**

24. As an administrator, I want to adjust a finding's start and end time, so that a skip does not cut off dialogue or leave part of a scene visible.
25. As an administrator, I want to type an exact timestamp, so that I can be frame-accurate when it matters.
26. As an administrator, I want to nudge a boundary by small increments, so that I can fine-tune without typing.
27. As an administrator, I want to set a boundary from the player's current position, so that I can scrub to the right frame and capture it.
28. As an administrator, I want to preview just the segment as trimmed, so that I can confirm the edit before saving.
29. As an administrator, I want to delete a finding outright, so that noise does not accumulate in the sidecar.
30. As an administrator, I want to add a finding the AI missed, so that my judgement is not limited to what a model detected.
31. As an administrator, I want to add a finding for a swear word the transcript missed, so that profanity coverage is not capped by transcription quality.
32. As an administrator, I want to choose the action for a finding — skip, mute or blur — so that a swear word is muted rather than the whole scene skipped.
33. As an administrator, I want to be told when an action only takes effect in a rendered copy, so that I do not add a mute expecting it to work during live playback.
34. As an administrator, I want my hand-made findings to survive a re-analysis, so that re-running an engine does not discard my work.
35. As an administrator, I want overlapping findings shown clearly, so that I can merge or trim them rather than stack redundant skips.

**Queueing analysis**

36. As an administrator, I want to queue a film for analysis from the grid, so that I do not need shell access to the worker.
37. As an administrator, I want to queue several films at once, so that I can set up an overnight run in one action.
38. As an administrator, I want to choose which engines run, so that I can take fast profanity results now and the slow visual pass later.
39. As an administrator, I want to see live progress and stage, so that I know whether a multi-hour visual pass is advancing.
40. As an administrator, I want to cancel a queued film, so that a mistaken queue does not occupy the GPU for hours.
41. As an administrator, I want to see the error when an analysis fails, so that I can fix the cause rather than guess.
42. As an administrator, I want films to appear for review as soon as their analysis completes, so that I can start reviewing without a manual refresh.

**Trust and safety**

43. As an administrator, I want review restricted to administrators, so that household users cannot approve or unapprove content.
44. As an administrator, I want the worker to stay unreachable from browsers, so that a Tailscale-only worker keeps working.
45. As an administrator, I want my decisions written to the sidecar next to the media, so that they survive the worker's database.
46. As an administrator, I want the original file never modified, so that review is always reversible.

## Implementation Decisions

### Item listing comes from Jellyfin, review state from the worker

The page lists libraries and videos by calling Jellyfin's own `Items` and
`Library/MediaFolders` APIs from the browser via `ApiClient` — same
origin, already authenticated, and the pattern Segment Editor uses. Only
worker-derived data is proxied through the plugin. This keeps unanalyzed
films visible (the worker knows nothing about them) and avoids
reimplementing library paging in C#.

### The plugin proxies all worker traffic

Every worker call goes through the plugin's controller, under the existing
`RequiresElevation` policy. Two reasons, both established earlier in the
project: the browser is a different origin from the worker, so direct
calls are blocked by CORS; and what matters operationally is whether the
*Jellyfin server* can reach the worker, because that is the machine that
fetches segments during playback.

### Status is a batch call, keyed by library item id

The page posts a page of item ids; the plugin resolves each to a file path
via the library manager and asks the worker once for all of them. One
round trip per screen, not one per film.

Job matching inside the status endpoint is by **file name, not
fingerprint**. Fingerprinting reads 24MB per film, which is unusable for a
whole library page. Fingerprint matching remains correct and stays in use
where it matters — duplicate-analysis detection.

Response shape per item: whether it has been analyzed, total findings,
approved / rejected / pending counts, and the latest job's status,
progress and stage if one exists.

### Path resolution is the worker's job, everywhere

Jellyfin knows a film as `/volume1/Media/Movies/Film.mkv`; the worker has
`C:/media/Film.mkv`. The worker already resolves this by matching file
name within configured media roots, rather than making administrators
maintain a mapping table. Every path-taking worker endpoint must use that
resolution — several did not, and 404'd on Jellyfin paths as a result.

### Video playback uses Jellyfin's stream, not worker clips

The editor plays the film from Jellyfin's own video stream endpoint,
authenticated and same-origin. This gives a full scrub bar over the whole
film — necessary for checking context around a finding and for setting a
boundary from the playhead. It also removes any need to proxy the worker's
clip builder for the editor.

The worker's clip endpoint stays for the standalone worker review page,
which remains supported for headless use.

### The sidecar remains the source of truth for decisions

Approve, reject, timing edits, deletions and additions all write to
`<media>.cleanmedia.json`. That file already drives both playback lookup
and rendering, and survives independently of the worker database.

### Segment mutation needs new worker API surface

Today a segment patch carries only approval and recommended action. The
editor requires:

- patching `startMs` / `endMs` on an existing finding,
- deleting a finding,
- creating a finding by hand.

Hand-created findings are marked with their own engine identity (rather
than an analysis engine's) so a re-analysis can merge fresh detections
without discarding manual work. Ids for new segments are allocated above
the existing maximum in the sidecar, never reusing a deleted id.

### Actions are labelled by where they take effect

Skip is honoured live by Jellyfin. Mute and blur require a rendered clean
copy — no Jellyfin client can apply them during playback. The editor shows
this per finding rather than accepting a mute that silently does nothing.

### Modules

| Module | Change | Depth |
| --- | --- | --- |
| Worker timeline lookup | Extracted from the segments endpoint so status and review share one definition of "every finding for this film" | Deep — one function, sidecar-or-merge, used by three callers |
| Worker status | New batch endpoint over the above | Deep — pure mapping from paths to counts |
| Worker segment mutation | New: edit timings, delete, create | Deep — sidecar in, sidecar out, no HTTP knowledge |
| Worker path resolution | Applied consistently across all path-taking endpoints | Existing, already deep |
| Plugin worker client | New status call; later, mutation calls | Shallow by nature — HTTP plumbing |
| Plugin controller | New proxy endpoints, id-to-path mapping | Shallow — a translation layer |
| Review page | New dashboard page: picker, grid, editor | UI |

## Testing Decisions

A good test here asserts external behaviour — given a sidecar and a
request, what comes back and what is on disk afterwards — not how the
code is structured internally. Tests should not assert on private
helpers, HTML markup, or the shape of intermediate data.

**Tested (agreed with the developer): the worker's status and segment
mutation modules.** This is where review decisions are persisted and where
a bug silently loses an administrator's work. Prior art already exists in
the repo: `tests/test_review.py` covers approval round-tripping through
the sidecar, and `tests/test_path_resolution.py` covers mapping a
foreign mount path onto a local file. New tests follow both directly.

Cases to cover:

- counts for an unanalyzed film, a partly reviewed film, and a fully reviewed one
- a path that resolves to nothing returns a well-formed "unknown" entry rather than an error
- a batch preserves request order, so callers may zip results positionally
- a queued or running job is reported alongside counts
- editing timings persists to the sidecar and leaves other fields untouched
- deleting a finding removes it and does not renumber the survivors
- a created finding gets an id above the existing maximum and survives a reload
- rejecting a finding keeps it in the sidecar but out of an approved-only lookup

**Not tested automatically:** the plugin controller (no C# test project
exists; adding one is not justified for a translation layer) and the
review page (browser tests against a live dashboard are the most brittle
and costly coverage available here). Both are verified by loading the
plugin in Jellyfin.

## Progress

### ✅ Slice 1 — Library grid with review state

Built, verified against real data, all 76 worker tests passing, plugin
builds clean with warnings-as-errors.

- ✅ Worker batch status endpoint, with file-name job matching
- ✅ Timeline lookup extracted and shared between status and segment lookup
- ✅ Path resolution applied to the thumbnail, clip, review-page and
  segment-approval endpoints — **all four were previously broken for
  Jellyfin paths**, having used the caller's path verbatim
- ✅ Plugin status proxy, resolving library item ids to file paths
- ✅ Review page registered as a dashboard page, in the main menu
- ✅ Library picker → poster grid, each card showing not analyzed /
  queued / analyzing *n*% / *n* of *m* to review / reviewed with
  approved and ignored counts
- ✅ Worker-unreachable message pointing at the settings page

Verified: `/volume1/Media/Movies/Hulk (2003).mkv` resolves to the local
file and reports 11 approved, 5 rejected, 3 pending.

- ✅ Grid filter: all / needs review / not analyzed / fully reviewed (story 10)

Deferred: sorting by pending count (story 11).

### ✅ Slice 2 — Per-film segment editor

Built. Covers stories 12–35.

- ✅ Worker: patch timings, delete, create; manual-finding identity
- ✅ Id allocation via a persistent high-water mark on the timeline
- ✅ Plugin: findings proxy (unapproved included) and mutation proxies
- ✅ Page: film view with the Jellyfin stream player and full scrub bar
- ✅ Per-finding row: typed timestamps and set-from-playhead for both bounds
- ✅ Approve / reject / retime / delete, each saved immediately
- ✅ Add a finding at the playhead, with a skip/mute/blur picker
- ✅ Mute and blur labelled inline as render-only, so they cannot silently no-op
- ✅ Next-undecided navigation, in-film decided count
- ✅ Clip fallback via the worker for containers the browser cannot decode
- ✅ 26 new tests

**Two real defects were found and fixed while building this**, both of
which would have destroyed an administrator's work:

1. *Re-analysis discarded everything it did not produce.* The batch path
   kept prior findings only for engines named in the current run, so
   hand-made findings — whose engine never runs — were dropped, as were
   another engine's results when re-running just one. The API path was
   worse: it overwrote the sidecar wholesale on every completed job.
   Both now merge, keeping engines that did not run, always keeping
   manual findings, and carrying settled decisions across on an
   engine-reference match.
2. *Segment ids were reused.* Ids came from `max + 1`, so deleting the
   newest finding handed its id to the next one created — and a review UI
   holding the old id would have acted on the wrong segment. Timelines now
   carry a high-water mark that survives deletion.

### ✅ Slice 3 — Queueing and progress

Built. Covers stories 36–42.

- ✅ Plugin: analyze proxy (batched), job listing, job cancellation
- ✅ Queue every film matching the current filter, with engine selection
- ✅ Live progress and stage in the grid, polled only while work is running
- ✅ Failed analyses surfaced on the card

Partial: cancellation exists as an endpoint but has no button in the grid
yet (story 40). Per-film queueing is only available via the filter — there
is no per-card queue button (story 36 is satisfied by the batch path).

## Out of Scope

- **Rendering clean copies from this UI.** Render already exists in the
  worker and has its own flow; wiring it into the review page is separate
  work.
- **Non-administrator access.** Review stays elevated-only. Per-user
  filtering profiles remain a v1 non-goal of the parent PRD.
- **Real-time mute and blur during playback.** Still impossible on every
  Jellyfin client; this PRD only makes the limitation visible.
- **Replacing the worker's standalone review page.** It stays for headless
  and debugging use.
- **A path-mapping configuration table.** Name-based resolution is the
  design; a mapping UI is only worth building if that proves insufficient.
- **Bulk approve-everything.** Deliberately absent — blanket approval
  without review is the failure mode the whole gate exists to prevent.
- **Reviewing on mobile or TV clients.** Dashboard only.

## Further Notes

Segment Editor and Intro Skipper are the reference implementations for a
plugin page that browses a library and edits per-item segments; both
confirm the dashboard can host this and are worth reading before slice 2.

The library grid deliberately shows films the worker knows nothing about.
An administrator's first question is "what still needs doing?", and a view
that only listed analyzed films could not answer it.

Story 34 — manual findings surviving re-analysis — is the one piece of
slice 2 with a real correctness trap. Merging fresh engine output into a
sidecar that contains hand-edited and hand-created segments needs a
deliberate rule, not incidental behaviour, and it should be settled before
the editor ships rather than after administrators have work to lose.
