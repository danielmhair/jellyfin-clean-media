# Changelog

All plugin releases, newest first. New entries are added automatically by the
release workflow from the notes in `plugin/CHANGELOG_NEXT.md`. The same notes
appear on the repo's [Releases page](../../releases) and in `manifest.json`.

<!-- releases -->

## 0.2.8.0 — 2026-08-15

Review library button + Studio review-page upgrades. The plugin page gains a '🎬 Review library' button that opens the worker's new library switcher — browse or search any film in any collection and open it for review, analyzed or not. Requires the matching worker update (restart the worker): the Studio review page now opens ANY video for hand review (scrub the whole film with audio, add cuts at the playhead) so analysis is an optional head-start, not a prerequisite; discreet mode now blurs the picture instead of hiding it, so you can play through a scene to find the edit without seeing the detail; and a top-left switcher (press /) jumps between films. Builds on 0.2.7.0.

## 0.2.7.0 — 2026-08-15

Flag from any device — new Remote tab. The plugin page gains a third tab, Remote, that lists what's playing on every client (including Roku and the mobile apps) with a 'Flag now' button and a 'Review' button for each. Flag captures a window at that device's current position as an unapproved finding, so you can mark a bad word or scene from your phone while it plays on the TV — no in-player button needed on native clients. Positions refresh a few times a second; the flag window and review retiming absorb the coarser reporting from set-top clients. Builds on 0.2.6.0.

## 0.2.6.0 — 2026-08-14

Confirm before removing a job. The X buttons in the queue (cancel a running pass, drop a queued job, remove a finished one) and 'Cancel all' now ask first, so a mis-click can't silently drop a job. The Remove dialog makes clear it only clears the list entry — the film's analysis and your review decisions are kept. Builds on 0.2.5.0's player buttons.

## 0.2.5.0 — 2026-08-14

Player buttons, hardened. Adds a second in-player button, 'Review this film', that opens the worker's review page for whatever is playing. The flag button's placement is now robust: it re-adds itself on a short interval and targets the main control bar, so it can no longer quietly vanish when Jellyfin re-renders the controls. Admin-only, browser (Jellyfin Web) only. Builds on 0.2.4.0's flag-a-moment button.

## 0.2.4.0 — 2026-08-14

Flag a moment during playback. A new flag button on the Jellyfin video player captures a short window around the current time (now +/- 1.5s, configurable) as an unapproved finding, so you can mark a bad word or scene while watching instead of hunting for it later in the review page. It appears in the review page as a manual finding to confirm, retime or classify — nothing acts during playback. Admin-only. The button is added by injecting a small script into the web client on startup; if the web directory is read-only the plugin log explains the one-line manual step. Also folds the old separate settings page into the tabbed Studio dashboard.

## 0.2.3.0 — 2026-08-10

Analysis schedule: restrict library analysis to chosen hours so it doesn't use the GPU while you're watching. A new Analysis schedule editor on the settings page sets a per-weekday allowed window (with a live allowed/paused status). The worker enforces it — queued passes wait for the window, and the long visual pass pauses at a checkpoint and resumes when it reopens; short profanity passes finish, and rendering is never delayed. Requires a worker restart to take effect. Builds on 0.2.2.0.

## 0.2.2.0 — 2026-08-10

Per-card Analyze and Cancel on the review grid: each film card now has its own button — Cancel while a pass is in flight (stops just that film's queued/running jobs), or Analyze otherwise. New one-click 'Analyze everything' (on the card and the film view) queues every engine a film still needs (profanity + visual) in one action, skipping anything already done or in flight. Builds on 0.2.1.3.

## 0.2.1.3 — 2026-08-10

Simpler film view: it's now a launcher — analyze, render a clean copy, or open the review page — with no per-finding list or player. All per-finding review (set mute/blur/skip, approve) happens on the worker review page, which gains a mute/blur/skip selector. Analyze is disabled unless an engine is ticked; clearer note that mutes/blurs need a render while skips work live.

## 0.2.1.2 — 2026-08-09

Film-view polish: an ETA next to each pass's percent; disable analysis engines already running/queued/done so you can't re-queue them; hide 'Next undecided' until there are findings and 'Add finding at playhead' unless the film can play; and a link to the worker's standalone review page. Builds on 0.2.1.1's live per-pass progress.

## 0.2.1.1 — 2026-08-09

Live analysis progress. The grid and film view now show each running pass with its own engine label and percent (a visual pass and a queued profanity pass no longer hide behind each other), with a progress bar; the film view shows progress on open without clicking Analyze. Builds on 0.2.1.0's 'Render clean copy' action.

## 0.2.1.0 — 2026-08-09

Render a clean copy from the film view. New 'Render clean copy' action applies approved mutes/blurs (and any approved skips) into a separate file via POST /api/render, reading the reviewed sidecar so only approved findings are acted on; the original is never modified. Live skips are unchanged.

## 0.2.0.5 — 2026-08-09

See the repository for changes.

## 0.2.0.4 — 2026-08-09

See the repository for changes.

## 0.2.0.3 — 2026-08-09

See the repository for changes.

## 0.2.0.2 — 2026-08-09

See the repository for changes.

## 0.2.0.1 — 2026-08-09

See the repository for changes.

## 0.2.0.0 — 2026-07-21

See the repository for changes.

## 0.1.0.2 — 2026-07-21

See the repository for changes.

## 0.1.0.1 — 2026-07-21

See the repository for changes.

## 0.1.0.0 — 2026-07-21

See the repository for changes.
