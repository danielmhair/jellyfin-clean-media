<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

- macOS: installing now adds a **"Clean Media Worker"** icon to the Desktop —
  double-click it any time to check whether the worker's running, restart it,
  change the media folder or add a second GPU machine, or view recent
  activity. `scripts/install-service.sh` gained a matching `--vlm-hosts` flag
  and `--restart` now actually re-applies changed settings instead of just
  kicking the existing process.
- The `.pkg` installer's progress window now closes itself when the install
  finishes successfully, and shows a clear "send this log to whoever gave you
  the installer" message (with the log file revealed in Finder) if it doesn't.
