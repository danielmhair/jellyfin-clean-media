#!/usr/bin/env bash
# Run the Clean Media worker as a macOS background service (a LaunchAgent).
#
#   scripts/install-service.sh                                # install + start
#   scripts/install-service.sh --media-roots "/Volumes/NAS/Movies:$HOME/Movies"
#   scripts/install-service.sh --vlm-hosts "http://localhost:11434,http://100.95.155.5:11434"
#   scripts/install-service.sh --restart                       # reapply current config + restart
#   scripts/install-service.sh --uninstall
#
# Without this, "leave a terminal window open running scripts/worker.sh" is
# the whole story on a Mac — no start-at-login, and nothing for an applied
# update (worker/update.py) or the Desktop icon (install.sh's last step) to
# restart into. A LaunchAgent (not a LaunchDaemon) runs inside your logged-in
# session, so it gets your normal Keychain and any already-mounted network
# shares — the same tradeoff install-service.ps1's -AtLogon mode makes on
# Windows, for the same reason.
#
# --restart re-*applies* the config, it doesn't just kick the existing
# process: pass --media-roots/--vlm-hosts alongside it to change them and
# restart in one step (matches install-service.ps1's -MediaRoots ... -Restart).
# Omit them on a --restart and this keeps whatever was already configured —
# it reads that back out of the existing plist rather than resetting to the
# hardcoded defaults below, so "just restart" never silently drops a NAS path.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

[ "$(uname -s)" = Darwin ] || {
  echo "install-service.sh only supports macOS (launchd). On Windows use" >&2
  echo "scripts/install-service.ps1; on Linux run scripts/worker.sh under your" >&2
  echo "own service manager (systemd --user, etc)." >&2
  exit 1
}

LABEL="com.cleanmedia.worker"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/Library/Application Support/CleanMedia"
LOG="$STATE_DIR/worker.log"

PORT=8765
MEDIA_ROOTS="$REPO/movies"
VLM_HOSTS=""
MEDIA_ROOTS_EXPLICIT=0
VLM_HOSTS_EXPLICIT=0
DO_UNINSTALL=0
DO_RESTART=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port)           PORT="$2"; shift 2 ;;
    --port=*)          PORT="${1#*=}"; shift ;;
    --media-roots)     MEDIA_ROOTS="$2"; MEDIA_ROOTS_EXPLICIT=1; shift 2 ;;
    --media-roots=*)   MEDIA_ROOTS="${1#*=}"; MEDIA_ROOTS_EXPLICIT=1; shift ;;
    --vlm-hosts)       VLM_HOSTS="$2"; VLM_HOSTS_EXPLICIT=1; shift 2 ;;
    --vlm-hosts=*)     VLM_HOSTS="${1#*=}"; VLM_HOSTS_EXPLICIT=1; shift ;;
    --uninstall)       DO_UNINSTALL=1; shift ;;
    --restart)         DO_RESTART=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "$STATE_DIR"

if [ "$DO_UNINSTALL" = 1 ]; then
  launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL. The worker will not start at login. Logs are left at $LOG."
  exit 0
fi

# --- locate uv ---------------------------------------------------------------
uv="$(command -v uv || true)"
if [ -z "$uv" ]; then
  for c in "$HOME/.local/bin/uv" "$HOME/.local/bin/uv.exe"; do
    [ -x "$c" ] && uv="$c" && break
  done
fi
[ -n "$uv" ] || { echo "uv not found — run scripts/install.sh first." >&2; exit 1; }

# --- a bare restart keeps whatever's already configured ----------------------
# Read values back out of the existing plist for anything not explicitly
# passed on this invocation, so `--restart` alone never resets a configured
# NAS path or VLM pool back to these hardcoded defaults.
if [ -f "$PLIST" ]; then
  if [ "$MEDIA_ROOTS_EXPLICIT" != 1 ]; then
    existing="$(sed -n 's#.*<key>CLEANMEDIA_MEDIA_ROOTS</key><string>\(.*\)</string>.*#\1#p' "$PLIST")"
    [ -n "$existing" ] && MEDIA_ROOTS="$existing"
  fi
  if [ "$VLM_HOSTS_EXPLICIT" != 1 ]; then
    existing="$(sed -n 's#.*<key>CLEANMEDIA_VLM_HOSTS</key><string>\(.*\)</string>.*#\1#p' "$PLIST")"
    [ -n "$existing" ] && VLM_HOSTS="$existing"
  fi
elif [ "$DO_RESTART" = 1 ]; then
  echo "not installed — run scripts/install-service.sh first (without --restart)" >&2
  exit 1
fi

# A pool of Ollama hosts for the visual pass (multi-GPU): fall back to what
# install.sh's --ollama existing flow saved, if nothing else set it.
if [ -z "$VLM_HOSTS" ] && [ -f "$REPO/.cleanmedia.env" ]; then
  VLM_HOSTS="$(sed -n 's/^export CLEANMEDIA_VLM_HOSTS="\(.*\)"$/\1/p' "$REPO/.cleanmedia.env")"
fi

# --- checks: warn, don't block ------------------------------------------------
# A NAS can be unmounted, or a GPU box turned off, at the exact moment this
# runs -- the worker itself already tolerates an unreachable root/host (see
# worker/review.py's media_roots()), so these are informational, not fatal.
step="Checking configured paths and hosts"
echo "==> $step"
IFS=: read -ra _roots <<< "$MEDIA_ROOTS"
for r in "${_roots[@]}"; do
  [ -n "$r" ] || continue
  if [ -d "$r" ]; then echo "  ok       media folder reachable: $r"
  else echo "  warn     media folder not found right now (unmounted?): $r"; fi
done
if [ -n "$VLM_HOSTS" ]; then
  IFS=, read -ra _hosts <<< "$VLM_HOSTS"
  for h in "${_hosts[@]}"; do
    [ -n "$h" ] || continue
    if curl -fsS --max-time 3 "$h/api/tags" >/dev/null 2>&1; then echo "  ok       vlm host reachable: $h"
    else echo "  warn     vlm host unreachable right now: $h"; fi
  done
fi

# --- (re)generate the plist and (re)start ------------------------------------
# Regenerating on every run (restart included) rather than special-casing
# --restart as a bare `launchctl kickstart` is deliberate: it's what makes
# --media-roots/--vlm-hosts passed alongside --restart actually take effect,
# and re-registering with launchd is cheap and idempotent either way.
VLM_ENV_XML=""
if [ -n "$VLM_HOSTS" ]; then
  VLM_ENV_XML="        <key>CLEANMEDIA_VLM_HOSTS</key><string>$VLM_HOSTS</string>"
fi

# Generated, not checked in: every value below is machine-specific.
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$uv</string>
        <string>run</string>
        <string>uvicorn</string>
        <string>worker.main:app</string>
        <string>--host</string><string>0.0.0.0</string>
        <string>--port</string><string>$PORT</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLEANMEDIA_MEDIA_ROOTS</key><string>$MEDIA_ROOTS</string>
$VLM_ENV_XML
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"
if [ "$DO_RESTART" = 1 ]; then
  echo "==> Restarted $LABEL with the config above."
else
  echo "==> Installed and started $LABEL."
fi
echo "  plist  $PLIST"
echo "  log    $LOG"

# --- verify the invariant, not the exit code ----------------------------------
echo "Waiting for the worker to answer on port $PORT (first start loads models, ~1-2 min)..."
version=""
for _ in $(seq 1 90); do
  body="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
  if [ -n "$body" ]; then
    # grep -o + head -1, not a greedy sed pattern: /api/health nests a
    # "version" per engine too (e.g. whisper reports faster-whisper's own
    # version), and a greedy .*"version":".* grabs the LAST match in the
    # string, not the worker's own version which comes first.
    version="$(printf '%s' "$body" | grep -o '"version":"[^"]*"' | head -1 | sed 's/^"version":"//; s/"$//')"
    break
  fi
  sleep 2
done

if [ -z "$version" ]; then
  echo "The worker did not answer within 3 minutes. Check the log:" >&2
  echo "  tail -n 40 \"$LOG\"" >&2
  exit 1
fi
echo "Worker $version is up."

if [ "$DO_RESTART" != 1 ]; then
  echo ""
  echo "Set the Worker URL in the Jellyfin plugin settings to one of:"
  for ip in $(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127\.'); do
    echo "    http://$ip:$PORT"
  done
fi
