<!-- Next plugin release changelog. Claude Code writes the user-facing notes here with each plugin change; the release workflow uses it as the changelog, then resets this file. Empty => the release falls back to the commit message. -->

- The worker now checks for newer releases and shows an **Update now** button
  on the settings page when one's available — nothing is ever applied
  without that click.
- macOS: `scripts/install.sh` can now set the worker up as a background
  service (`scripts/install-service.sh`), and every release now includes a
  double-clickable `CleanMedia.dmg` installer for friends who'd rather not
  use a terminal.
