# Spec — Review page "Studio" refactor (D variant)

Status: **implemented & in daily use** (Phase 1 + Phase 2 + playback/scrub/UX
polish) · Date: 2026-08-13 (updated 2026-08-14) · Area: worker review UI
(see [Implementation status](#implementation-status-2026-08-13) at the end for
what shipped and the remaining open items. The Studio page has fully **replaced**
the old review grid; the regression table below is historical.)
Reference prototype: [plan/prototypes/review_prototype.html](../prototypes/review_prototype.html)
(variant **D**, the target). Supersedes the current `PAGE` template in
[worker/review.py](../../worker/review.py).

## Problem Statement

The administrator review page works but reads as a hobbyist grid, and it fights
the person actually using it. Concretely:

- **A parent can't review privately.** The page shows a thumbnail of every
  flagged scene, so reviewing objectionable content means displaying it — often
  with the kids it's meant to protect in the room. There's no way to *find* the
  edit point without replaying the bad scene over and over.
- **The decision controls are counter-intuitive.** A green check means "the
  detection was correct" and a red ✕ means "not a bad scene" — but a red ✕
  reads as "cut this part." Reviewers second-guess every click.
- **Real edits need surgery the UI can't do well.** One flagged "bad scene"
  often contains a critical story beat in the middle that should be *kept*; the
  reviewer needs to cut the parts around it — one scene becomes several
  micro-segments. Adding, splitting, retiming, recategorising and re-actioning
  those pieces is clumsy today.
- **You can't see where you are.** Findings are a flat list; there's no
  continuous view of the film, no sense of where a finding sits, and the timing
  editor is a tiny window pinned to one finding rather than a scrubbable view of
  the surrounding footage.

## Solution

Replace the review page with a single **Studio** workspace (prototype variant
D): a continuous **monitor** that always follows a **playhead**, a full-film
**minimap** whose viewport **box** drives a zoomable **editor** below (filmstrip
+ waveform stacked), and a left rail of **findings** that shows each finding's
description inline and decides it in one click. The reviewer works the playhead;
everything else follows it.

Two framing changes:

- **Discreet mode** (on by default, because the actor is a parent): the monitor
  is blurred and the picture hidden, so the reviewer navigates by description,
  waveform and position — not by watching the content — with a hold-to-reveal
  escape hatch.
- **Decision language keyed to the viewer's outcome**: **✂ Cut out** (red — the
  content is removed from view) and **👁 Leave in** (green — it plays normally).
  Red now means "removed," matching the intuition the old ✕ violated. Deleting a
  finding entirely is a separate, explicit action, not the reject button.

Crucially, this is a **presentation refactor**: "Cut out" is still
`approved: true` and "Leave in" is still `approved: false` in the
`.cleanmedia.json` **sidecar**. The **timeline** schema, the
`GET /api/segments?approvedOnly=true` contract, and the Jellyfin plugin are
unchanged.

## User Stories

1. As a reviewing parent, I want the picture hidden by default (discreet mode),
   so that I can review objectionable content without others in the room seeing
   it.
2. As a reviewing parent, I want to hold a button to briefly reveal the frame,
   so that I can confirm a detail when I need to without leaving the picture up.
3. As a reviewing parent, I want each finding's description shown in the list
   without clicking (e.g. "blood/wound detail across 5 shots"), so that I know
   what a finding is without displaying it.
4. As a reviewer, I want to scrub the film and find an edit point by waveform and
   position, so that I don't have to replay the bad scene repeatedly to place a
   cut.
5. As a reviewer, I want a decision control that reads "Cut out" (red) vs
   "Leave in" (green), so that the colour and label match what the viewer will
   experience.
6. As a reviewer, I want "Cut out" and "Leave in" on each list row, so that I can
   settle findings without opening the editor.
7. As a reviewer, I want a progress bar of cut / left-in / to-review, so that I
   can see how much of the film I've triaged.
8. As a reviewer, I want a continuous monitor that always shows what's at the
   playhead, so that I have one coherent view instead of per-finding clips.
9. As a reviewer, I want the monitor and left rail to highlight the finding
   nearest the playhead as I scrub, so that I always know which finding I'm on.
10. As a reviewer, I want a full-film minimap with a marker per finding, so that
    I can see where findings cluster and jump anywhere in the film.
11. As a reviewer, I want the minimap to show a box for the region the editor
    below is zoomed into, so that I understand the relationship between the map
    and the detail view.
12. As a reviewer, I want to drag that box to pan the editor, so that the minimap
    acts as a map for the zoomable timeline.
13. As a reviewer, I want the box to shrink and grow as I zoom the editor, so
    that the map always reflects the current zoom.
14. As a reviewer, I want the editor to always show the filmstrip and waveform
    stacked, so that I can scrub picture and audio together in any scene,
    whether the finding is visual or spoken.
15. As a reviewer, I want to zoom the editor timeline with the scroll wheel
    (around the cursor), so that I can go from scene overview to frame-level
    placement quickly.
16. As a reviewer, I want the editor filmstrip to mark each analysed shot, so
    that I can see the shot boundaries the visual pass used.
17. As a reviewer editing one flagged scene, I want to add a new cut at the
    playhead with one key/click (defaulting to skip), so that I can remove a
    part the engines missed.
18. As a reviewer, I want to split a region at the playhead, so that I can carve
    one scene into several micro-segments.
19. As a reviewer, I want to keep a story beat in the middle of a bad scene by
    splitting around it and deleting the middle piece, so that a critical moment
    survives while the parts around it are cut.
20. As a reviewer, I want each region/finding's action switchable between skip,
    mute, voice-only mute and blur, so that I can choose how each part is
    treated.
21. As a reviewer, I want mute/voice/blur regions marked "render-only", so that
    I understand they only take effect in a rendered clean copy, not live in
    Jellyfin.
22. As a reviewer, I want to edit a finding's timestamps by typing
    H:MM:SS.mmm, so that I can move an item precisely and quickly.
23. As a reviewer, I want ±1s and ±25ms nudge buttons on the start and end, so
    that I can fine-tune bounds without dragging.
24. As a reviewer, I want to drag a region's edges and body on the timeline, so
    that I can retime it by eye.
25. As a reviewer, I want to edit a finding's category (nudity, gore, profanity,
    …), so that I can correct a mis-categorised detection, not just its action.
26. As a reviewer, I want to edit a finding's description, so that I can correct
    a stale note on a duplicated or split finding.
27. As a reviewer, I want to merge several findings of the same type into one
    span, so that a scene flagged shot-by-shot becomes a single decision.
28. As a reviewer, I want the merge control to refuse a mix of types, so that I
    don't accidentally collapse unrelated findings.
29. As a reviewer, I want to Shift+drag on the editor to highlight a span in
    blue and audition it, so that I can check what's there before moving a
    handle. (Audio playback lands with the audio-scrub follow-up; the visual
    selection + monitor frame work now.)
30. As a reviewer, I want the editor lane to dim regions I've marked "Leave in",
    so that the lane reflects my decision, not just the action.
31. As a reviewer, I want the editor result line to tell me how much the current
    view removes and whether the gaps keep the beat, so that I can confirm the
    carve does what I intend.
32. As a reviewer, I want to delete a finding outright from the editor, so that I
    can remove noise the engines produced.
33. As a reviewer, I want keyboard controls — J/K move between findings, Space
    play/pause, A add cut, S split, Del delete region, C cut out, L leave in —
    so that I can triage a long film quickly.
34. As a reviewer, I want the monitor/viewport to follow the playhead during
    playback (auto-panning to keep it in view), so that the detail view tracks
    what's playing.
35. As a reviewer, I want clicking a finding in the list to seek to it and centre
    the editor on it, so that I can jump straight to the thing I want to edit.
36. As a reviewer, I want approvals, edits, splits, merges and deletes to persist
    to the sidecar immediately, so that my decisions survive a reload and reach
    Jellyfin.
37. As the Jellyfin plugin, I want the sidecar and `approvedOnly=true` contract
    unchanged, so that approved skips still surface as Commercial segments with
    no plugin change.

## Implementation Decisions

**Which page this is.** The target is the **worker** review page — the `PAGE`
template rendered by `render_page` in the review module and served at
`GET /api/review?path=…`, which opens in its own tab and is where per-film
findings are actually edited. It is **not** the plugin's dashboard page
(`plugin/…/Configuration/reviewPage.html`), which is the in-Jellyfin library
grid / queue / progress UI and merely `window.open`s the worker's
`/api/review` URL. The plugin page is untouched: it keeps linking to the same
URL, and the refactored page loads in its place.

**Front-end is the bulk.** Replace the `PAGE` template rendered by
`render_page` in the review module with the D-variant Studio workspace. The
reference implementation (structure, CSS, interaction wiring) is the D section
of the prototype file; port it, don't reinvent it. The page stays a single
self-contained server-rendered HTML/JS document with no external dependencies,
consistent with the current page.

**Decision state is presentational only.** The cut/leave/undecided tri-state
maps directly onto the existing `approved` field — no schema change, no sidecar
migration:

```
approved === true   →  "Cut out"   (red)    — acted on (skip live; mute/blur render-only)
approved === false  →  "Leave in"  (green)  — dismissed, plays normally
approved === null    →  undecided   (grey)
```

**Viewport / minimap model** (encoded by the prototype): the editor renders an
independent time window `[viewStart, viewEnd]` over the film, mapped to the
pane's on-screen width. The minimap box is `[viewStart, viewEnd] / runtime`.
Wheel zoom changes the *span* around the cursor (not a scroll); dragging the box
pans the span; the box width is a pure function of the span, so it shrinks/grows
with zoom. The playhead is the single source of focus: scrubbing sets `playMs`,
which drives the monitor frame, the nearest-finding highlight in the rail, and
(during playback) auto-panning of the viewport.

**Carve = ordinary sidecar edits.** Splitting a finding is "shorten the original
+ create a new finding for the second half"; adding a cut is a create; keeping a
beat is deleting the middle piece. These reuse the existing create / patch /
delete / merge endpoints. No new "carve" persistence concept is introduced — a
micro-segment is just a segment, and a kept beat is the absence of one.

**One API contract change: editable category.** Add an optional `category`
field to the segment-patch input model and have the update function apply it
when explicitly sent (same "only apply fields actually sent" rule as the
existing timing/reasoning edits). Everything else the UI needs already exists:
create, delete, bulk-approve, merge, and per-finding patch of
approved/action/timing/reasoning.

**Viewport peaks/filmstrip reuse the existing media endpoints.** The stacked
filmstrip and waveform for an arbitrary viewport are served by the existing
peaks and filmstrip endpoints, called with the viewport window as the
start/end and zero pad (they already compute over `[startMs-pad, endMs+pad]`).
No finding id is required — the window is just a time range. Results remain
cached/cacheable as today.

**Monitor: frame-follow now, audio-scrub later.** The continuous monitor shows
the frame at the playhead using the existing thumbnail endpoint (cheap, cached),
and per-finding audio uses the existing clip endpoint. Smooth real-time audio as
you scrub arbitrary positions requires on-the-fly transcode of the (often
MPEG-2) source and is an explicit follow-up (see Out of Scope). The UI marks the
scrubbing state so the seam is visible where audio will attach.

**Merge stays same-type in the UI.** The merge endpoint is unchanged; the UI
only enables merge when the picked findings share one category, and shows the
resulting type. (The endpoint already tolerates arbitrary ids; the constraint is
a UI affordance, matching the "merge findings of one type" story.)

**No change to:** the sidecar/timeline schema, the `MANUAL_ENGINE` merge
semantics, the `approvedOnly` read the plugin uses, the job/queue system, or the
render path.

## Testing Decisions

**Primary seam: full end-to-end Playwright tests, no mocking.** Each feature is
covered by a high-fidelity test that drives the real review page served by the
worker against a real (synthesised) media file and asserts on the **observable
outcome**, including what lands in the sidecar — never on internal DOM/JS
structure. A good test here reads as a reviewer's action → a persisted result:
e.g. "click Cut out on a finding → reload → the finding shows Cut out and the
sidecar has `approved: true`"; "split a region at the playhead → the sidecar now
has two findings covering the original span with a gap where the middle was
deleted"; "edit category to gore → sidecar category is gore"; "merge two
profanity findings → one finding spans both, originals gone."

**Fixture: a synthesised media file, not a gitignored real movie.** A test
fixture generates a short deterministic `.mkv` with ffmpeg (a test pattern +
tone) and writes a matching `.cleanmedia.json` sidecar, so the E2E suite is
self-contained and reproducible and never depends on the gitignored `movies/`.
This keeps the "verify the invariant" discipline: the tests exercise real
ffmpeg-backed peaks/filmstrip/thumbnail/clip paths, not stubs.

**Coverage, one test per feature area:** discreet-mode default + hold-to-reveal;
inline descriptions; decision cut/leave/undecided persistence (row and editor);
progress bar counts; minimap markers + click-to-seek; viewport box pan; wheel
zoom resizing the box; stacked filmstrip+waveform present for both a visual and
a spoken finding; add-cut; split; keep-the-beat (split-split-delete leaves a
gap); action switch incl. render-only marking; typed timestamp edit; ±1s/±25ms
nudges; edge/body drag retime; category edit; description edit; same-type merge
+ mixed-type refusal; keyboard shortcuts; nearest-finding highlight while
scrubbing; delete finding.

**Manual `playwright-cli` pass now.** Before sign-off, a manual `playwright-cli`
walkthrough confirms the page works today against the worker (the project's
"reports success, produces garbage" failure mode means the invariant is checked
in a real browser, not just by green unit tests). The gitignored
`.playwright-cli/` scratch dir is used for this.

**Retain the existing unit seam.** The worker-function tests
([tests/test_review.py](../../tests/test_review.py)) stay green and gain a case
for the new editable `category` patch field — prior art for how to assert a
patch persists to the sidecar is already in that file.

## Out of Scope

- **Real-time audio-scrub over arbitrary positions.** Smooth continuous audio as
  the playhead drags anywhere in the film needs an on-the-fly transcoded/
  segmented streaming endpoint for the (often MPEG-2) source. This spec ships the
  visual scrub (frame-follow + waveform) and per-finding audio via the existing
  clip endpoint; the streaming endpoint is a defined follow-up seam.
- **The other prototype variants (A, B, C).** They were exploration; only D is
  the target. They are not ported and should not ship on `main`.
- **Any change to the Jellyfin plugin, the sidecar/timeline schema, the job
  queue, or the render engines.** The refactor is confined to the worker review
  page and the single `category` patch addition.
- **The "analyze for everything" one-click job, word-lock timing model, and
  voice-only Demucs work** tracked elsewhere in PROGRESS.md.
- **Two-level minimap** (a coarse film map + a medium neighbourhood strip). At
  deep zoom the single minimap box is a few pixels wide; a min-width keeps it
  grabbable. If that proves too fiddly, a second map tier is a follow-up.

## Further Notes

- The prototype is the design source of truth for layout, spacing, colour system
  (one colour + glyph per category, severity-ranked) and interaction feel. It
  was verified rendering correctly and driving its flows (split → two findings;
  wheel zoom → box resize; scrub → nearest highlight) via automation.
- Capture the prototype as a primary source on a throwaway branch when the
  refactor lands; it should not remain on `main`.
- Known refinements deliberately left as open calls in the prototype, to decide
  during the port: whether the viewport auto-follow during playback should pause
  while an input field is focused (avoid yanking mid-edit), and the exact
  min-width/behaviour of the minimap box at extreme zoom.
- **Tracker note:** this session had no `gh` CLI and no configured triage-label
  vocabulary, so the spec is filed here in `plan/prds/` (the project's spec
  home) rather than as a GitHub issue. To publish it as a `ready-for-agent`
  issue, install `gh` / configure the tracker and re-run the publish step.

## Implementation status (2026-08-13)

The Studio page is **built, wired to the real endpoints, and tested end to
end.** What follows is the honest ledger for the manual test pass: what works,
what regressed against the old page ([docs/FEATURES.md](../../docs/FEATURES.md)
§"Review UI"), and the follow-ups.

### Done and verified

- **The whole D-variant workspace is ported** into `PAGE` / `render_page` in
  [worker/review.py](../../worker/review.py): discreet monitor, full-film
  minimap + draggable viewport box, wheel-zoom, zoomable editor (real filmstrip
  + waveform stacked), findings rail with inline descriptions and one-click
  cut/leave, progress bar, merge mode, keyboard controls. `render_page` now
  injects via token replacement instead of `str.format` (the JS is brace-heavy).
- **Real media, not gradients.** Monitor frame → `/api/thumbnail` (debounced);
  editor filmstrip → `/api/filmstrip`, waveform → `/api/peaks`, both called with
  the viewport window and `pad=0`, peaks cached per-window, both skipped past a
  180 s window so a full zoom-out doesn't hammer ffmpeg.
- **Editable category** — the one API change: `SegmentPatch.category`,
  `update_segment(category=…)`, wired into the by-path PATCH endpoint. Unit +
  API tests added.
- **Everything persists** through the existing create/patch/delete/merge
  endpoints; structural ops refetch the timeline. Verified in the sidecar:
  cut/leave/undecided, category, action, description, typed time, nudges,
  edge/body drag, add, split, keep-the-beat, delete, same-type merge,
  mixed-type refusal. Reload-persistence and the `approvedOnly=true` plugin read
  both confirmed.
- **Tests:** a Python-Playwright E2E suite ([tests/e2e/](../../tests/e2e/)) —
  real worker subprocess against a synthesised `.mkv` + sidecar, 25 tests, one
  per feature area, asserting on the sidecar. Unit seam retained + category
  case. **189 passed** (164 unit + 25 e2e). Run with
  `uv run playwright install chromium` then `uv run pytest tests/e2e`.
- **Fixed during testing:** the monitor's colour-gradient stand-in darkened the
  *real* frame when discreet mode was off (it fades to opaque black); it now
  shows only in discreet mode, and the centred note is cleared when the picture
  is up (the transport line carries context).

### Regressions vs the old page — clip playback is gone

The old page's biggest capability was **real `<video>` playback via
`/api/clip`**. The Studio page shows **still frames only** and does **not wire
`/api/clip` at all**, so these features from FEATURES.md §"Review UI" have **no
equivalent** in the new page yet:

| Old feature (FEATURES.md) | Status in Studio | Alternative today |
|---|---|---|
| **Play scene** — browser-playable clip with **audio**, seeking to the flagged part | ✗ gone | frame-follow only; no audio |
| **Preview skip** — play the run-up then jump the span the way Jellyfin skips it | ✗ gone | none |
| **Play muted** — hear the scene as it plays once muted (confirm the word) | ✗ gone | none |
| **Play voice-removed** — hear the Demucs voice-only mute before approving | ✗ gone | none |
| **Audition** — Shift+drag a span and hear just that; "Use as bounds" | ~ partial | visual select + playhead move only; **no audio**, no "Use as bounds" |
| **Filters & bulk** — filter by decision/type, bulk-approve what's shown | ✗ gone | merge mode groups adjacent findings; per-row decide + progress counts, but no filtered bulk-settle |
| **Duplicate finding** | ✗ gone | add-cut then retime/retype |

Note the spec itself intended **per-finding audio to stay** (§"Monitor:
frame-follow now, audio-scrub later" — *"per-finding audio uses the existing
clip endpoint"*). Only smooth audio while scrubbing **arbitrary** positions was
deferred (§Out of Scope). So the loss of Play/Preview-skip/Preview-muted is a
gap against this spec's own intent, not just against the old page — the port
dropped the clip wiring entirely rather than carrying it onto the selected
finding.

> **Update (2026-08-13): this table is now largely historical.** Playback with
> audio (Play / Cleaned = skip-jump + mute/voice / Muted) and type filtering +
> bulk have been **restored** — see the [Follow-up spec](#follow-up-spec--monitor-playback--type-filtering-agreed-not-yet-built)
> below, marked BUILT — including **whole-film continuous** playback (Phase 2,
> the `Scene ↔ Film` toggle + `/api/stream`). Still missing by choice:
> **Duplicate finding**.

## Follow-up spec — monitor playback + type filtering (agreed, not yet built)

Two capabilities from the old page are being **restored, redesigned to fit the
Studio model** (the reviewer works one playhead; everything follows it). These
are the requirements from the product owner after the first manual look. Both
must land. Nothing here changes the sidecar/timeline schema or the plugin
contract.

### A. Playback with audio — a Visual/Video picture toggle + three audio modes

The monitor must actually **play with sound**, not just step through still
frames. Two independent axes: what the **picture** shows, and what the **audio**
does.

**Picture toggle — Visual ↔ Video (audio plays in both):**

- **Visual** (default, parent-friendly): the screen shows the **frames**
  (filmstrip/thumbnail imagery), not moving video, while the **audio plays**. So
  a reviewer hears the scene — enough to place a profanity mute or judge a
  moment — without the moving picture up. Discreet mode still blurs/hides the
  frame here; you review by ear + waveform + position.
- **Video**: the **real video plays with audio** — the moving picture.
- **Scrubbing shows the picture moving through, in both modes** — dragging the
  playhead updates the frame (Visual) or seeks the video (Video) continuously,
  so you *see* the scrub, cut regions included.

**Audio mode — three explicit choices, all wanted** (product owner: *"Play with
normal audio; play with audio but silence audio that we state to skip; play with
audio muted. We want all"*):

1. **Normal** — the original audio, nothing applied. Hear the scene as shot.
2. **Cleaned (acted-on)** — the reviewer's **approved decisions applied live**:
   spans set to **skip are jumped**, spans set to **mute/voice are silenced** (a
   voice-only span drops the vocals). This is the **"verify the invariant"**
   preview — hear/see exactly what the viewer will get, and confirm a mute lands
   on the word, *before* approving. (Kept as its own mode — confirmed, not folded
   into the plain mute.)
3. **Muted** — audio off entirely; picture only.

Follow-up user stories:

38. As a reviewer, I want a **Visual/Video** picture toggle where **audio always
    plays**, so that in Visual mode I can *hear* a scene (and place a mute)
    without the moving picture up, and in Video mode I can watch it in full.
39. As a reviewer, I want to choose the **audio mode — Normal / Cleaned / Muted**,
    so that I can hear the scene as shot, hear it **as the viewer will get it**
    (skips jumped, mutes silenced) to verify my edits, or silence it entirely.
40. As a reviewer, I want **scrubbing to show the picture moving through** in both
    Visual and Video modes, so that I can find an edit point by eye as well as by
    waveform. Scrubbing is **never** skip-jumped, so I can always move into and
    edit a cut region.

**Scope decision (product owner leans whole-film; confirm the phasing):** the
target is **whole-film continuous playback** — hold play and the film runs with
sound across scenes at any playhead. That needs a **new transcoded/segmented
streaming endpoint** for the (often MPEG-2) source (the one previously in Out of
Scope), and "Cleaned" over the whole film means applying skips/mutes **live**
during the stream — effectively a real-time render. Two ways to get there:

- **Phase 1 — per-window, now (no new backend):** all three audio modes and both
  picture modes work **within the selected finding / viewport window**, reusing
  the existing `/api/clip` (it already returns a seekable MP4 with audio and
  supports `mute`/`voice`/skip). This restores the old page's verification power
  immediately, scoped to the scene you're editing — which is where review
  actually happens.
- **Phase 2 — whole-film streaming:** the new endpoint for continuous audio at
  an arbitrary playhead, with live cleaned playback. Larger backend piece.

**Decided (2026-08-13):** build **Phase 1 first** (per-window, via the existing
clip endpoint), ship and test it, then **Phase 2 (whole-film streaming) is the
end goal** as its own task after Phase 1 is validated. In Phase 1, "Cleaned"
applies the **selected finding's** action (skip-jump / mute / voice) within its
window — the old per-finding verification; multi-region and whole-film cleaned
playback are Phase 2. In **Video** mode, pressing play **reveals** the picture
even under discreet mode (an explicit action); **Visual** mode always stays
private (audio + blurred/hidden frames).

**Other implementation notes / seams:**

- **Visual mode = audio + frames** is implementable by playing the clip element
  for its audio while the on-screen image is driven from `currentTime` →
  filmstrip/thumbnail (video track hidden), keeping the moving picture off.
- **Cleaned mode** reuses the old clip cut-marker + "jump the span" behaviour for
  skips and the `mute`/`voice` clip flags for mutes, generalised to every
  approved region in the window.
- **Discreet interaction:** Visual mode with discreet on = audio + blurred/hidden
  frames (the private review path); Video mode is an explicit reveal. Confirm
  whether Video playback is blocked while discreet is on, or reveals on press.

### B. Type filter that scopes the whole workspace, with bulk cut/leave

Bring back type filtering, but make it **scope the entire Studio**, not just a
list:

- A **type filter** (chips per type, as the old page had — each profane word its
  own group, categories otherwise). Selecting a type filters **everything at
  once**: the **left rail**, the **full-film minimap markers**, and the
  **editor / main timeline regions** all show only that type.
- A **bulk "Cut out all" / "Leave in all"** acting on exactly the
  currently-filtered (shown) findings — so a reviewer can filter to one word and
  settle the whole group in one click. Reuses the existing bulk PATCH
  (`/api/segments {ids, approved}`); one request, one sidecar write.
- Clearing the filter restores the full film.

Follow-up user stories:

42. As a reviewer, I want to **filter findings by type**, and have the rail, the
    minimap and the editor **all show only that type**, so that I can focus the
    whole workspace on one kind of content at a time.
43. As a reviewer, I want to **"Cut out all" (or "Leave in all") of the filtered
    type** in one action, so that I can settle a whole group (e.g. every instance
    of one word) at once.

**Status: BUILT and tested (2026-08-13).** Both A (Phase 1) and B shipped in
[worker/review.py](../../worker/review.py):

- **B — type filter + bulk:** type chips in the rail scope the rail, the minimap
  markers, and the editor regions together; a bulk **✂ Cut all / 👁 Leave all**
  acts on the filtered set via the existing bulk PATCH. E2E: filter scopes
  rail+minimap, filter scopes editor regions, bulk-cut-all, bulk-leave-all.
- **A Phase 1 — playback with audio:** a **Visual/Video** picture toggle and a
  **Normal / Cleaned / Muted** audio control on the monitor, playing the selected
  finding's window through the existing `/api/clip` (real audio, seekable). A
  hidden `<video>` carries the audio in Visual mode (frame image on top);
  pressing play in Video mode reveals. Cleaned applies the finding's action
  (skip→jump, mute/voice→silenced); Muted mutes the element. Shift+drag audition
  now plays its span's real audio. E2E: video-mode reveal / visual-private,
  play-loads-clip, cleaned-applies-mute, muted-mutes.
- **Gotcha fixed in the port:** the `<video>` initially had `preload="none"`, so
  setting `src` never triggered a load and `onloadedmetadata` (which calls
  `play()`) never fired — a silent deadlock. Fixed with `preload="metadata"` +
  an explicit `v.load()`. (A textbook "reports success, produces nothing" bug —
  caught in the browser, not by the green DOM checks.)

**Cleaned playback, refined (2026-08-13):** two fixes after real-movie testing.
- **Cleaned now applies *every* cut in the window, not just the selected finding**
  (the bug: it only skipped the highlighted scene). The set is every **cut-out
  (approved) finding** in the window **plus the one you're working on**; skips are
  cut, mutes/voice silenced.
- **Cleaned no longer transcodes the footage it cuts** (the "(a)" optimization).
  A new `GET /api/preview_clip?…&cut=<spans>&mute=<spans>` builds a *windowed
  render* — `build_preview_clip` inverts the cut spans into keeps and
  trim+concats them, so a big skip's middle is never encoded. On the fixture a
  10 s window with a 4 s cut builds a 6 s clip in ~0.4 s. The clip's time is
  compressed, so the page maps clip-time→film-time through the keeps (the
  playhead jumps across the cut). E2E: cleaned-cuts-a-skip, cleaned-applies-mute,
  cleaned-skips-every-cut-in-the-window; unit: build_preview_clip compresses by
  the cut length. (Voice-only is *silenced* in this quick multi-region preview,
  not Demucs-separated — noted as an approximation.)

### Phase 2 — whole-film continuous streaming (BUILT, 2026-08-13)

The end goal: **hold play and the film runs continuously across scenes**, with
sound, from any playhead — and "Cleaned" over the **whole** film, not just a
scene window. Built and tested end to end (including real in-browser playback).

- **New endpoint `GET /api/stream?path=…&startMs=…&cut=…&mute=…`** — transcodes
  the film from `startMs` to the end into a **fragmented MP4** (`frag_keyframe+
  empty_moov`) streamed live on stdout, so a plain `<video>` starts playing in a
  second or two and runs across scene boundaries. `stream_command()` builds the
  ffmpeg argv (a straight transcode for Normal/Muted; a whole-remaining-film
  `build_preview_clip`-style keeps-concat for Cleaned, so cut footage is never
  encoded). Seeking is the page's job — it reloads the endpoint from a new
  `startMs`; **each stream owns one ffmpeg process, killed on client disconnect**
  (async endpoint + `is_disconnected()` check) so a re-seek never orphans a
  transcode stealing the GPU. Empirically verified: ffmpeg dies within ~3 s of
  the client dropping.
- **A `Scene ↔ Film` range toggle** on the monitor. **Scene** keeps Phase 1's
  fast, cached per-window clip (tight verification of the finding you're
  editing). **Film** streams the whole film from the playhead. Both picture
  modes (Visual/Video) and all three audio modes (Normal/Cleaned/Muted) apply in
  Film mode; the active mode is the one that streams — Muted is Normal with the
  element muted, so it reuses the same stream. Cleaned-Film gathers **every
  approved (cut-out) skip from the playhead to the end** and silences all
  approved mutes/voice, mapping stream-time→film-time through the same keeps the
  windowed preview uses (`D_buildKeeps`), so the playhead jumps across cuts.
- **Playback follow** — a pause/resume is cheap (the loaded element just plays on
  if it still sits at the playhead); a click or seek moves the playhead, so the
  next play re-streams from there. `±1s` and timeline clicks stop playback and
  move the playhead (a heavy live transcode isn't restarted per nudge); Space
  resumes from the new position.
- **Tests:** `stream_command` unit tests (a straight-transcode Normal argv; a
  cleaned stream **run for real** and probed — a valid MP4 compressed by the cut
  length); `/api/stream` API guards (404 unknown media, 416 start-past-end);
  E2E — Film streams the whole film from the playhead **and playback actually
  advances past the finding in a real browser**, Film+Cleaned carries every
  approved cut ahead. **207 passed.**

### Live scrub audio (BUILT, 2026-08-14)

The one deferred audio piece — **hearing the film as you drag the playhead** — is
now built, so a reviewer can find an edit point by ear, not just by waveform and
frame. Implemented with **WebAudio grains**, not a re-stream per drag:

- **New endpoint `GET /api/scrub_audio?path=…&startMs=…&endMs=…`** —
  `build_scrub_audio` extracts a compact **mono 22 kHz WAV** of the window
  (audio-only, cheap, cached). The page decodes it once with
  `decodeAudioData`.
- **Grain engine** (`D_scrubAudio` / `D_saGrain`): a scrub mousedown primes the
  `AudioContext` (the gesture that lets it play), and each drag move plays a
  faded grain at the playhead — continuous DAW-style scrub sound. A ±45 s window
  is buffered around the cursor and only refetched when the drag nears its edge,
  so a slow scrub reuses the buffer and only a big jump reloads. Wired into
  **both** the editor scrub and the full-film minimap scrub; skipped in Muted
  mode.
- **Single voice, no echo** (2026-08-14 fix): overlapping grains sounded like an
  echo, so a new grain now **stops the previous one** with a tiny fade
  (`D_saKill`) — one voice follows the cursor. Released on scrub-up (`D_saStop`).
- **Tests:** `build_scrub_audio` unit (a real, decodable mono WAV of the right
  length); `/api/scrub_audio` 404 guard; E2E — a real editor drag runs the whole
  gesture→AudioContext→fetch→decode path and lands a decoded buffer. **210
  passed.**

### Cleaned shows blur, and an editor scrollbar (BUILT, 2026-08-14)

- **Blur in Cleaned** — a finding set to **blur** is now blurred in the Cleaned
  preview and stream, using the render's own full-frame `gblur=sigma=30`
  (`BLUR_SIGMA` imported from `render.py`, so preview == clean copy). Both
  `build_preview_clip` and `stream_command` take a `blurs` span list and apply
  it before the scale/split; a blur-only decision no longer takes the
  straight-transcode path. New `blur=` query param on `/api/preview_clip` and
  `/api/stream`; the page gathers blur spans in `D_cleanedPlan`/`D_filmPlan`.
  Tests: unit (preview builds a blurred, full-length clip; stream argv contains
  the enable-gated gblur) + E2E (Cleaned play on a blur finding sends
  `blur=8000-11000`).
- **Editor scrollbar** — a horizontal scrollbar under the zoomed editor whose
  **thumb is the viewport over the whole film**: drag it to pan, click the track
  to jump — so you can move without reaching up to the minimap. E2E: dragging the
  thumb advances `viewStart`.

**Approximations still open:** voice-only spans are *silenced* in the cleaned
stream (not Demucs-separated — the per-play, whole-film case can't afford the
separation pass); the dedicated voice preview stays a per-finding Scene concern.
Scrub audio plays the **source** audio at the position (not the cleaned/muted
mix) — it's a locator, not a cleaned preview. **Duplicate finding** remains
separately restorable if wanted, but is lower value now that add-cut + retime
covers it.

### Region-retime scrubs, app shell, and scrollbar steps (BUILT, 2026-08-14)

The last round of manual-testing feedback, all in [worker/review.py](../../worker/review.py):

- **Retiming a region is a scrub.** Dragging a region's edge or body now moves
  the **playhead with the edge** (`D_dragScrub`) and plays the scrub-audio grain
  + updates the frame as it goes, so a cut bound is placed by ear and eye —
  "scrubbing with the segment landing right" — instead of retimed blind. The
  earlier stale-track-ref safety is intact (the follow calls `D_monitor`/`D_filmtl`,
  which don't rebuild the editor).
- **Fixed-viewport app shell — no page scrollbar.** The studio is now a
  `height:100vh` flex column with `overflow:hidden`; only the findings rail and
  the stage scroll, inside their own bounds. The old layout used a stale fixed
  height and grid items without `min-height:0`, so the stage grew to its content
  and pushed the page past the viewport — stacking a page scrollbar on top of the
  internal ones and hiding the editor below a fold. **All scrollbars now share one
  slim rounded style** (`::-webkit-scrollbar` + Firefox `scrollbar-color`). The
  monitor was trimmed (44vh → 34vh) so the editor timeline + tools sit in view.
  E2E asserts the page can't scroll (`scrollHeight ≤ innerHeight`).
- **Fine-scrub step buttons.** The editor's horizontal pan-scrollbar is flanked
  by **◀/▶ buttons** that nudge the playhead a little bit (250 ms) per click with
  scrub audio + frame, **hold to repeat** — a fine complement to the transport's
  ◀1s/1s▶ and the ±25 ms field nudges.

### Also still open (from the spec)

- **Real-time audio-scrub is now BUILT** — via WebAudio grains (see *Live scrub
  audio*), not the streaming/transcode approach the spec had deferred. Continuous
  whole-film **playback** uses the streaming endpoint (Phase 2); *scrubbing*
  audio uses decoded WAV grains. Both directions are covered.
- **Shot marks** (story 16) are drawn at the visual findings' boundaries in
  view, not from a true PySceneDetect shot list (no shots endpoint exists). A
  real shot overlay is a small follow-up if wanted.
- **Duplicate finding** was not re-added (add-cut + retime covers it); voice-only
  spans are *silenced*, not Demucs-separated, in the multi-region cleaned preview.
- Capturing the prototype on a throwaway branch and removing it from `main`.
