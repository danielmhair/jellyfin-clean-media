#!/usr/bin/env bash
# Profanity only — about a minute per film once subtitles are cached.
#
# Worth running across a whole library first: it is thousands of times
# faster than the visual pass, and tells you which films are worth the
# hours of GPU time.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

if [ $# -eq 0 ]; then
  echo "usage: $0 <file|folder|glob> [...]" >&2
  exit 1
fi

"$UV" run python -m worker.batch --engines subtitles "$@"
