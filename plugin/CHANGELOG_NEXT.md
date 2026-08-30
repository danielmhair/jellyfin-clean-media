<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

## Fixed: one damaged review file could blank out the whole review page

If a single film's `.cleanmedia.json` review file was damaged — cut short by
an interrupted write, or edited by hand and left invalid — opening the review
page (or the collection grid's per-film progress dots) could fail for your
**entire library**, showing nothing at all instead of just that one film. A
damaged file is now reported as its own status ("unreadable") so every other
film still lists and opens normally.

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
