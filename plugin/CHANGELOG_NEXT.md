<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

- Windows: `install-service.ps1` now also adds a **"Clean Media Worker"**
  Desktop icon — the same status/restart/settings menu macOS got — and
  `-Restart` genuinely re-applies a changed `-MediaRoots`/`-VlmHosts` instead
  of silently ignoring them.
