# Changelog

Every release of Clean Media, newest first. The worker and the Jellyfin plugin
are released together as a matched pair: each version below is one git tag
(`vX.Y.Z`) containing both, and the plugin is published as `X.Y.Z.0`. To run a
specific version, check out its tag (or download its GitHub Release) and install
the matching plugin from `manifest.json`.

New entries are added automatically by the release workflow from the notes in
`plugin/CHANGELOG_NEXT.md`. The same notes appear on the repo's
[Releases page](../../releases) and in `manifest.json`.

<!-- releases -->
## 0.2.12.0 — 2026-08-25

Move a queued job straight to the top or bottom. Each job in "Up next" now has move-to-top (⤒) and move-to-bottom (⤓) buttons, so you can jump a film to the front of the queue or push it to the back in one click — no dragging. The buttons grey out when a job is already at the top or bottom.

## 0.2.11.0 — 2026-08-24

The clean copy now shows up as a "Clean" version of the movie in Jellyfin. When a film lives in its own folder (the standard Jellyfin layout), the render now writes the clean copy right beside the original as a "… - Clean" file, which Jellyfin groups as a selectable version of that same movie — so you pick "Clean" in the player instead of hunting for a separate file, and the original is never modified. Films that aren't in their own folder (a flat library, or TV episodes) can't be grouped this way, so their clean copy still goes into a "cleaned" subfolder as before. After a render finishes, Clean Media asks Jellyfin to do a quick library scan so the new version appears on its own.

See where the clean copy went, and jump to it. Finished renders in the queue's "Recent" list now show where the clean copy was written (full path on hover) and have an "Open in Jellyfin" button that takes you straight to that film's page — where the Clean version is ready to play.

## 0.2.10.0 — 2026-08-24

See at a glance what's been analyzed. Each film in "Add to queue" now shows three small coloured dots — for the subtitle, audio (whisper), and visual passes — lit in the pass's colour when that analysis is done, faintly outlined when it hasn't run, and gently pulsing while a pass is running for that film. A small key above the list explains the colours. So you can scan a whole collection and tell what still needs work without opening each film.

Jump straight to review from the queue dialog. The per-film "Add to queue" dialog now has a "Review…" button that opens that film's findings editor directly, so you can go from picking passes to reviewing results in one click.

A queued render no longer looks like it's running. With a single graphics card only one job runs at a time, so a render you queue waits its turn behind an analysis pass. Previously, hiding a pass type with the "Show" filters could make that waiting render jump up to "Running now" and appear to be running when it wasn't. The queue now decides what's actually running from the real job state, so a render correctly stays in "Up next" until it truly starts, and filtering the view never changes whether something looks like it's running.

Tidier queue rows. Queue rows now have a consistent minimum height, so shorter entries no longer look cramped next to ones with a progress bar.

## 0.2.9.0 — 2026-08-21

Worker/plugin version handshake. The worker and plugin are now released together as a matched pair — one version, one download set — so you install a set that's known to work together. The plugin's settings page now checks the worker it connects to: if the two are out of step it shows a clear "version mismatch" warning that names which side is behind and the one command to fix it, instead of failing quietly. When they match you'll just see the worker version as before. Restart the worker after updating so it reports its new version.

Much faster visual analysis on small graphics cards. The vision pass now loads the whole model onto the GPU by default instead of leaving part of it on the CPU — measured about 3.8× faster on a 4 GB card. Nothing to configure; it just runs quicker.

Voice-only mute now previews correctly. When you set a finding to "Voice-only mute" and played it in the review page, the Cleaned preview was silencing the whole span — music and all — instead of removing just the spoken word. The scene preview now runs the real vocal-removal so you hear exactly what the clean copy will sound like: the word gone, the music and ambient playing through. (The whole-film continuous preview still hard-mutes those spans, since it can't separate audio on the fly.)

Use a second machine's GPU for the visual pass. You can now point the worker at more than one Ollama server, and a single film's frames are spread across all of them at once — so a second PC's graphics card roughly doubles the speed. Set it once on the worker with CLEANMEDIA_VLM_HOSTS (a comma-separated list of Ollama URLs), or pass --hosts to the analyze command. The pool balances itself: a faster card simply does more frames, and if one machine goes offline mid-run the pass keeps going on the others and picks up the rest on the next run.

Fixed a rare freeze in the multi-GPU visual pass. If a frame failed to extract or decode while analysis was spread across more than one graphics card, the pass could stop making progress and sit stuck instead of moving on. It now treats that frame as a skip (retried next run) and keeps going, so the pass can't stall.

One-command setup for a fresh machine. A new `scripts/install.sh` gets a new user from a downloaded copy of the project to a working worker in a single step — it installs everything the worker needs (uv and its private Python, FFmpeg, Ollama, and the vision model), only filling in what's missing and never touching what you already have, then prints the two remaining steps (install the plugin from the manifest URL, start the worker). It runs on macOS, Linux, and Windows (in Git Bash), and can point at an Ollama you already run elsewhere with `--ollama existing --ollama-host <url>`.

Easier multi-GPU setup. A new `scripts/vlm-hosts.sh` manages the pool of Ollama servers the visual pass fans across — `add`, `remove`, `set`, `clear`, and a `list` that probes every host and tells you which are ready (reachable and have the model). It saves the pool where the worker reads it on start, so you no longer hand-edit an environment variable.

Lighter "discreet" blur on the review page. Discreet mode now softens the picture less heavily, so you can still tell what's happening in a scene while you review and edit it, instead of it being blurred almost to nothing.

Queue page fits on one screen. On the queue tab, "Running now", "Up next", and "Recent" each scroll within their own area now, so all three stay visible at once instead of pushing each other down a long page — and the "Add to queue" collection list scrolls the same way. A busy queue no longer means scrolling the whole page to see what finished.

Pick exactly what to run, per film. Clicking a film in "Add to queue" now opens a dialog that shows what's already been done for it — which passes have finished, what's still running or queued, and how many findings are approved or waiting for review — and lets you tick the passes you want (profanity from subtitles, profanity from audio, the visual scene pass, and rendering a clean copy) and queue them all in one click. So "analyze for everything" is now a few checkboxes instead of adding each pass separately, and you won't accidentally re-queue something that's already done.

Sort the film list. The "Add to queue" film picker now has a sort control with the usual choices — name, release date (newest or oldest), recently added, community rating, and runtime — so a big library is easier to work through.

Analysis no longer fails on a brief network-drive hiccup. When your films live on a NAS or network share, a momentary dropped read used to fail a whole job — profanity analysis stopping with an "Invalid argument" error, or the visual pass refusing to run because shot detection only saw a fraction of the film. The worker now retries these reads a few times before giving up, so a passing glitch on the share no longer wastes a long run. A file that's genuinely unreadable still reports the same clear error, just after a couple of quick retries.


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
