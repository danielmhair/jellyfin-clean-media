<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

- Fixed re-running a "Done" whisper or visual pass finishing suspiciously fast and changing nothing — the previous fix only stopped the queue from handing back the same old job, but whisper's own cached transcript and the visual pass's own saved progress were still silently reused underneath it. Re-running a pass now genuinely starts over for both.
