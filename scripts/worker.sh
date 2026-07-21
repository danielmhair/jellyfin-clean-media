#!/usr/bin/env bash
# Start the worker API.
#
# CLEANMEDIA_MEDIA_ROOTS lets Jellyfin ask about a film by its own path
# (/volume1/Movies/...) and still match the local copy by file name.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

PORT="${PORT:-8765}"
export CLEANMEDIA_MEDIA_ROOTS="${CLEANMEDIA_MEDIA_ROOTS:-$PWD/movies}"

echo "media roots: $CLEANMEDIA_MEDIA_ROOTS"
echo "listening on http://0.0.0.0:$PORT"
"$UV" run uvicorn worker.main:app --host 0.0.0.0 --port "$PORT"
