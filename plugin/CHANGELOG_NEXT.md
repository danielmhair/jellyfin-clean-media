<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->
See at a glance what's been analyzed. Each film in "Add to queue" now shows three small coloured dots — for the subtitle, audio (whisper), and visual passes — lit in the pass's colour when that analysis is done, faintly outlined when it hasn't run, and gently pulsing while a pass is running for that film. A small key above the list explains the colours. So you can scan a whole collection and tell what still needs work without opening each film.

Jump straight to review from the queue dialog. The per-film "Add to queue" dialog now has a "Review…" button that opens that film's findings editor directly, so you can go from picking passes to reviewing results in one click.

A queued render no longer looks like it's running. With a single graphics card only one job runs at a time, so a render you queue waits its turn behind an analysis pass. Previously, hiding a pass type with the "Show" filters could make that waiting render jump up to "Running now" and appear to be running when it wasn't. The queue now decides what's actually running from the real job state, so a render correctly stays in "Up next" until it truly starts, and filtering the view never changes whether something looks like it's running.

Tidier queue rows. Queue rows now have a consistent minimum height, so shorter entries no longer look cramped next to ones with a progress bar.
