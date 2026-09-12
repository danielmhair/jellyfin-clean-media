<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

- Fixed a clean copy silently losing its audio's language tag and "default" marking whenever the render included a cut scene (not just a mute/blur) — Jellyfin would show the clean version's audio with no language and not pre-selected. The clean copy's audio is now tagged and marked default again, matching the original.
