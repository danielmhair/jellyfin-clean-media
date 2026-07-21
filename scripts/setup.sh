#!/usr/bin/env bash
# Install dependencies and verify the external tools each engine needs.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

echo "==> installing python dependencies"
"$UV" sync

echo "==> re-applying pureframe patches (uv sync overwrites them)"
"$UV" run python patches/apply_patches.py

echo "==> checking external tools"
check() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "  ok       $1 — $2"
  else
    echo "  MISSING  $1 — $2"
  fi
}
check ffmpeg   "required by every engine"
check ffprobe  "required by every engine"
check tesseract "OCR of DVD subtitles (winget install UB-Mannheim.TesseractOCR)"
check ollama   "vlm engine (ollama pull qwen3-vl:4b-instruct)"
check dotnet   "building the Jellyfin plugin"

echo "==> done"
