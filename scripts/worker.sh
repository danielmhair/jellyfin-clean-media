#!/usr/bin/env bash
# Start the worker API.
#
# CLEANMEDIA_MEDIA_ROOTS lets Jellyfin ask about a film by its own path
# (/volume1/Movies/...) and still match the local copy by file name.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

# Config persisted by scripts/install.sh / scripts/vlm-hosts.sh — e.g. the
# Ollama pool (CLEANMEDIA_VLM_HOSTS) or a remote host. Sourced (not overridden)
# so an env var set on the command line still wins.
[ -f "$PWD/.cleanmedia.env" ] && source "$PWD/.cleanmedia.env"

PORT="${PORT:-8765}"
export CLEANMEDIA_MEDIA_ROOTS="${CLEANMEDIA_MEDIA_ROOTS:-$PWD/movies}"

echo "media roots: $CLEANMEDIA_MEDIA_ROOTS"
[ -n "${CLEANMEDIA_VLM_HOSTS:-}" ] && echo "vlm hosts:   $CLEANMEDIA_VLM_HOSTS"
echo "listening on http://0.0.0.0:$PORT"
"$UV" run uvicorn worker.main:app --host 0.0.0.0 --port "$PORT"
