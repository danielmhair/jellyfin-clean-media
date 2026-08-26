#!/usr/bin/env bash
# Move clean copies from the legacy cleaned/ subfolder to the "<movie> - Clean"
# version path beside the original, so Jellyfin groups them as selectable
# versions of the movie. Dry run by default.
#
#   scripts/migrate-clean-copies.sh                     # show what would move
#   scripts/migrate-clean-copies.sh --apply             # actually move them
#   scripts/migrate-clean-copies.sh --apply "\\Nas\Movies"   # explicit root(s)
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

# Pick up any roots persisted to .cleanmedia.env. Deliberately DON'T default
# CLEANMEDIA_MEDIA_ROOTS here — if it's unset (the Windows service bakes it into
# its own launcher, not this shell), the tool asks the running worker for its
# configured roots on /api/health, so no path needs typing.
[ -f "$PWD/.cleanmedia.env" ] && source "$PWD/.cleanmedia.env"

"$UV" run python -m worker.migrate_clean_copies "$@"
