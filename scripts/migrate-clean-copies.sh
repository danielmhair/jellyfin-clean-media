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

# Same config the worker starts from, so we scan the same media roots.
[ -f "$PWD/.cleanmedia.env" ] && source "$PWD/.cleanmedia.env"
export CLEANMEDIA_MEDIA_ROOTS="${CLEANMEDIA_MEDIA_ROOTS:-$PWD/movies}"

"$UV" run python -m worker.migrate_clean_copies "$@"
