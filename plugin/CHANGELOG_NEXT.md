<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

## A profanity pass and a visual pass can now run at the same time

The worker used to run one analysis job at a time no matter what it was, so
queuing a visual (VLM) pass for one film and a profanity (whisper) pass for
another meant the second just waited its turn. Now those two kinds of work run
in parallel automatically — visual analysis still runs one film at a time
(using every configured GPU host for that one film, same as before), and so
does profanity analysis, but the two no longer block each other.

## Fixed: some videos could never finish a profanity (whisper) pass

A handful of videos — ones spliced from more than one source, such as a DVD
rip with extra scenes cut into the main feature — always failed the whisper
profanity pass partway through, even after every retry, with an error
claiming a dropped network read. It wasn't actually the network: playback
software can read past the splice point fine, but a straight sequential
transcription trips on it every time. The transcription step now extracts
the audio with ffmpeg first, and if that comes up short, picks back up right
after the trouble spot and stitches the two pieces together, instead of
retrying the exact same read and failing the same way again. Re-queue the
audio pass on any film that previously failed this way.

## Fixed: one damaged review file could blank out the whole review page

If a single film's `.cleanmedia.json` review file was damaged — cut short by
an interrupted write, or edited by hand and left invalid — opening the review
page (or the collection grid's per-film progress dots) could fail for your
**entire library**, showing nothing at all instead of just that one film. A
damaged file is now reported as its own status ("unreadable") so every other
film still lists and opens normally. Opening *that* film's own review page no
longer shows a bare server error either — it names the exact file to fix and
explains that the film itself is untouched. And saving a review decision now
writes safely (to a temp file, then swapped in) instead of overwriting the
file in place, so a save interrupted by a worker restart can no longer leave
a half-written, damaged file behind in the first place.

## Fixed: freshly analyzed films could sit unlit for up to 30 minutes

If you ran the audio/visual analysis tool on a different machine than the one
running the worker (for example, over Tailscale to a GPU box), the review
grid's per-film progress dots could keep showing a film as "not analyzed" for
up to half an hour after analysis actually finished, because the nudge that
tells the worker to refresh could only ever reach a worker on the same
machine. The analysis tool now accepts `--worker-url` (or a
`CLEANMEDIA_WORKER_URL` environment variable) so it can reach a worker running
elsewhere, and prints why the nudge failed instead of failing silently.

## Render a clean copy from the review page

The review page now has a **Render clean copy** button in its header. You no
longer have to go back to the queue page to act on the findings you just
approved. It offers the same choice — overwrite the copy you have, or add
another one beside it — and the button turns into a progress readout while the
render runs.

## Type an exact time on the playhead clock

Click the time above the player and type where you want to be: `1:37.950`,
`12:04`, or plain seconds. Dragging gets you near a moment; placing a mute on
one word needs the moment itself.
