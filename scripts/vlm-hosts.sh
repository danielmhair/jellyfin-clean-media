#!/usr/bin/env bash
# Configure a POOL of Ollama servers so the visual pass fans out across several
# GPUs (yours, or friends' boxes on the network) instead of one card.
#
# The vlm engine already splits one film's frames across every host in the pool
# — a faster card simply pulls more work, so the pool self-balances a 4 GB and a
# 12 GB card with no tuning (see worker/engines/vlm_engine.py). This script just
# manages the host list the worker reads from, CLEANMEDIA_VLM_HOSTS.
#
#   scripts/vlm-hosts.sh add http://192.168.1.9:11434 http://192.168.1.20:11434
#   scripts/vlm-hosts.sh set http://localhost:11434,http://192.168.1.9:11434
#   scripts/vlm-hosts.sh list          # show the pool and probe each host
#   scripts/vlm-hosts.sh remove http://192.168.1.9:11434
#   scripts/vlm-hosts.sh clear         # back to single local Ollama
#
# After changing the pool, restart the worker so it picks the change up
# (scripts/worker.sh, or install-service.ps1 -Restart on the Windows service).
#
# EACH host in the pool must be reachable AND have the same model pulled:
#   - on every GPU box:  set OLLAMA_HOST=0.0.0.0:11434 so it listens on the LAN,
#     restart Ollama, open port 11434 in its firewall, then `ollama pull <model>`.
# `list` probes all of that for you and says which hosts are ready.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="$PWD/.cleanmedia.env"
MODEL="${VLM_MODEL:-qwen3-vl:4b-instruct}"

have() { command -v "$1" >/dev/null 2>&1; }

# --- read/write the persisted host list -----------------------------------

read_hosts() {
  [ -f "$ENV_FILE" ] || return 0
  # Pull the value out of `export CLEANMEDIA_VLM_HOSTS="a,b"`, strip quotes.
  sed -n 's/^export CLEANMEDIA_VLM_HOSTS=//p' "$ENV_FILE" | tail -1 | tr -d '"'"'"
}

# Normalise a comma/space/newline separated list: trim, drop trailing slashes,
# drop blanks and duplicates while keeping order.
normalise() {
  tr ', ' '\n\n' | while read -r h; do
    h="${h%/}"; [ -n "$h" ] && echo "$h"
  done | awk '!seen[$0]++'
}

write_hosts() { # newline-separated hosts on stdin
  local csv; csv="$(paste -sd, -)"
  # Preserve any other lines the install script wrote; replace only our var.
  local tmp; tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ]; then
    grep -v '^export CLEANMEDIA_VLM_HOSTS=' "$ENV_FILE" > "$tmp" || true
  else
    echo "# Written by scripts/vlm-hosts.sh — the Ollama pool the worker uses." > "$tmp"
  fi
  [ -n "$csv" ] && echo "export CLEANMEDIA_VLM_HOSTS=\"$csv\"" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
  if [ -n "$csv" ]; then
    echo "pool -> $csv"
  else
    echo "pool cleared (worker falls back to a single local Ollama)"
  fi
  echo "restart the worker to apply (scripts/worker.sh, or install-service.ps1 -Restart)."
}

current() { read_hosts | normalise; }

# --- probe one host: reachable? model present? ----------------------------

probe() { # base-url
  local url="$1" tags
  if have curl;   then tags="$(curl -fsS --max-time 4 "$url/api/tags" 2>/dev/null)" || { echo "  DOWN     $url"; return; }
  elif have wget; then tags="$(wget -qO- --timeout=4 "$url/api/tags" 2>/dev/null)"  || { echo "  DOWN     $url"; return; }
  else echo "  ?        $url (no curl/wget to probe with)"; return; fi
  if printf '%s' "$tags" | grep -q "\"$MODEL\""; then
    echo "  READY    $url"
  else
    echo "  NO MODEL $url — run: OLLAMA_HOST=${url#http://} ollama pull $MODEL"
  fi
}

# --- commands --------------------------------------------------------------

cmd="${1:-list}"; shift || true

case "$cmd" in
  list)
    hosts="$(current)"
    if [ -z "$hosts" ]; then
      echo "pool: (none set) — the worker uses a single local Ollama at http://localhost:11434"
      echo "$MODEL:"
      probe "http://localhost:11434"
    else
      echo "pool ($(echo "$hosts" | grep -c .) host(s)), probing for $MODEL:"
      echo "$hosts" | while read -r h; do probe "$h"; done
    fi
    ;;
  add)
    [ $# -ge 1 ] || { echo "usage: vlm-hosts.sh add <url> [url...]" >&2; exit 1; }
    { current; printf '%s\n' "$@"; } | normalise | write_hosts
    ;;
  remove|rm)
    [ $# -ge 1 ] || { echo "usage: vlm-hosts.sh remove <url> [url...]" >&2; exit 1; }
    drop="$(printf '%s\n' "$@" | normalise)"
    current | grep -vxF -f <(echo "$drop") | write_hosts || echo "" | write_hosts
    ;;
  set)
    [ $# -ge 1 ] || { echo "usage: vlm-hosts.sh set <url,url,...>" >&2; exit 1; }
    printf '%s\n' "$@" | normalise | write_hosts
    ;;
  clear)
    printf '' | write_hosts
    ;;
  -h|--help|help)
    sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "unknown command: $cmd (try: list, add, remove, set, clear)" >&2
    exit 1
    ;;
esac
