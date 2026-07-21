#!/usr/bin/env bash
# Analyze films: profanity first (fast), then the visual pass (hours).
#
#   scripts/analyze.sh movies/
#   scripts/analyze.sh "movies/Some Film (2010).mkv"
#   OLLAMA_HOST_URL=http://100.95.155.5:11434 scripts/analyze.sh movies/
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

if [ $# -eq 0 ]; then
  echo "usage: $0 <file|folder|glob> [...]" >&2
  exit 1
fi

args=(--engines subtitles,vlm)
[ -n "${OLLAMA_HOST_URL:-}" ] && args+=(--host "$OLLAMA_HOST_URL")
[ -n "${VLM_MODEL:-}" ] && args+=(--model "$VLM_MODEL")

"$UV" run python -m worker.batch "${args[@]}" "$@"
