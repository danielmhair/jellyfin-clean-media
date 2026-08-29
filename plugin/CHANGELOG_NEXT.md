<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

## Flagging while you watch the clean copy now works

Spotting a missed word while watching the "Clean" version is the normal way
things get found, but a flag made there used to be recorded against the clean
copy itself — and since every render rebuilds that copy from the film's approved
findings, the next render quietly threw the flag away. Flags now go on the film,
so they survive and are applied the next time you render.

If the copy you're watching has scenes cut out of it, the flagged moment is
shifted back to where it really is in the film rather than being taken at face
value. Copies rendered from now on record exactly what was cut, so that shift is
exact. For a copy made before this release there is no such record, so the
worker compares the two files' lengths instead: if they match, nothing was cut
and the timing is used as-is; if the copy is shorter and your approved cuts
account for the difference, those are used. When the numbers don't add up, the
flag still lands on the film but arrives **undecided** with a note explaining
why — a moment nobody can place shouldn't come pre-approved, since acting on it
would cut or mute the wrong scene.

Rendering also reads the film itself now, never a copy of a copy, so a re-render
applies everything you've approved and doesn't re-encode an encode.

## Rendering asks before overwriting a clean copy

When a clean copy already exists you now choose:

- **Overwrite "Clean"** — replace the copy you have.
- **Keep it, add "Clean 2"** — leave the old copy playable and add another
  version alongside it.

Either way the existing copy stays intact until the new one is finished, so a
render that fails part-way through can no longer leave you with a broken file
where a working clean copy used to be.

The queue page can now aim at a specific version of a film. A rendered clean
copy is an alternate version of the movie rather than a library entry of its
own, so it never showed up in the film list; films with more than one version
now get a picker, and the dialog tells you exactly which file the clean copy
will be written to before you add anything to the queue.

Opening a clean copy in the review page now opens the film it was made from —
that is where the findings live and where a decision has to land to survive the
next render.

## New Settings and Advanced tabs — configure the worker without a terminal

Media roots, the VLM host pool, what counts as nudity/suggestive/kissing for
each category, and profanity word options are now editable right from the
plugin's Settings and Advanced tabs — no more SSH/RDP into the worker machine
and re-running an install script. Changes take effect on the next analysis job,
no restart needed. Media roots gets its own folder browser so you can pick your
library location without typing a path by hand.

## Restart the worker from the plugin

A "Restart worker" button on the Settings page can now start or restart the
worker even if it's become unresponsive — and, on a fresh install, even if the
worker process is completely down. It's safe to use anytime: any analysis in
progress resumes automatically rather than starting over. This relies on a
small always-on helper that install-service now sets up alongside the worker
(macOS and Windows for this release; Linux support is still to come) — re-run
install-service once to pick it up on an existing install. It can be turned off
from the Advanced tab if you'd rather manage the worker yourself.
