#!/usr/bin/env bash
# Shared bootstrap. Source this, do not run it.
#
# uv installs to ~/.local/bin, which is not on PATH in a fresh Git Bash
# or a non-login shell — so locate it rather than assuming.

repo_root() { cd "$(dirname "${BASH_SOURCE[1]}")/.." && pwd; }

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi
  for candidate in \
    "$HOME/.local/bin/uv" "$HOME/.local/bin/uv.exe" \
    "${LOCALAPPDATA:-}/Programs/uv/uv.exe" \
    "${USERPROFILE:-}/.local/bin/uv.exe"
  do
    [ -x "$candidate" ] && { echo "$candidate"; return; }
  done
  return 1
}

UV="$(find_uv)" || {
  echo "uv not found. Install it:" >&2
  echo "  winget install astral-sh.uv       # Windows" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh   # Linux/macOS" >&2
  exit 1
}
export UV
