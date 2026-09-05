<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

## Loading feedback everywhere something used to just sit there

A lot of actions across the queue page and the review page used to give no
sign anything was happening until it suddenly finished — cancel/requeue/
reorder buttons in the queue, opening the film picker's search or collection
list, loading the Settings tab, the library switcher and waveform in the
review page, and a few more. They now show a spinner or a disabled/busy
state while the request is in flight, so a slow connection or a big library
reads as "working" instead of "stuck."

## Fixed: the "add a film" picker's dots didn't update after queuing

Queuing a pass from the film picker used to leave that film's progress dots
showing whatever they were before — you'd have to close and reopen the
picker to see it light up as running. The dots now refresh right after a
successful queue.

## The "add a film" picker's progress dots light up much faster

Opening the film picker (to queue a new analysis pass) used to check each
film's status one at a time — for a page of films that are mostly already
analyzed, that meant one file read after another before any dots would light
up, which could take a long time on a NAS share. The worker now checks
several films at once, so the dots fill in together instead of trickling in.

## Review clips now load much faster

Pressing play on a finding in the review page used to make you wait several
seconds to over ten before the clip would even start, because the worker
transcoded that scene on the CPU before sending any video back. It now uses
the same GPU encoder the clean-copy render already relies on, so a clip is
ready well before you'd notice the wait.

## Fixed: the queue tab got slower to load the more of your library you'd analyzed

The queue page polls the worker every few seconds while it's open, and that
poll used to fetch every job the worker had ever run — so after analyzing a
lot of films, each poll (and the "recent" list it renders) kept growing and
growing. The worker now only sends the finished jobs from your last 20 or so,
plus everything still queued or in progress, so the poll stays fast no matter
how much of your library you've analyzed.

## A profanity pass now waits its turn if it would fight the visual pass for the same GPU

A profanity (whisper) pass and a visual (VLM) pass already run at the same
time when they can. But if your only GPU is doing the visual pass, starting a
profanity pass on that same machine meant the two fought over one card
instead of genuinely running side by side. The profanity pass now waits (and
its queue entry says why) until the visual pass frees up this machine's GPU —
a second machine helping the visual pass over the network doesn't count, so
that case still runs the two in parallel as before.
