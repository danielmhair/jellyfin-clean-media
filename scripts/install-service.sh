#!/usr/bin/env bash
# Run the Clean Media worker as a macOS background service (a LaunchAgent).
#
#   scripts/install-service.sh                                # install + start
#   scripts/install-service.sh --media-roots "$HOME/Movies"    # custom media root
#   scripts/install-service.sh --restart                       # pick up new code
#   scripts/install-service.sh --uninstall
#
# Without this, "leave a terminal window open running scripts/worker.sh" is
# the whole story on a Mac — no start-at-login, and nothing for an applied
# update (worker/update.py) to restart into. A LaunchAgent (not a
# LaunchDaemon) runs inside your logged-in session, so it gets your normal
# Keychain and any already-mounted network shares — the same tradeoff
# install-service.ps1's -AtLogon mode makes on Windows, for the same reason.
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
DO_UNINSTALL=0
DO_RESTART=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port)           PORT="$2"; shift 2 ;;
    --port=*)          PORT="${1#*=}"; shift ;;
    --media-roots)     MEDIA_ROOTS="$2"; shift 2 ;;
    --media-roots=*)   MEDIA_ROOTS="${1#*=}"; shift ;;
    --uninstall)       DO_UNINSTALL=1; shift ;;
    --restart)         DO_RESTART=1; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

if [ "$DO_RESTART" = 1 ]; then
  [ -f "$PLIST" ] || { echo "not installed — run scripts/install-service.sh first" >&2; exit 1; }
  echo "Restarting $LABEL..."
  launchctl kickstart -k "$DOMAIN/$LABEL"
else
  # A pool of Ollama hosts for the visual pass (multi-GPU), if install.sh
  # saved one — see scripts/install.sh's --ollama existing flow.
  VLM_HOSTS=""
  if [ -f "$REPO/.cleanmedia.env" ]; then
    VLM_HOSTS="$(sed -n 's/^export CLEANMEDIA_VLM_HOSTS="\(.*\)"$/\1/p' "$REPO/.cleanmedia.env")"
  fi
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

  # Reinstall is the normal case (re-running this script, or a first install
  # over a previous one) — bootout a stale copy before registering the new one.
  launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  launchctl enable "$DOMAIN/$LABEL"
  launchctl kickstart -k "$DOMAIN/$LABEL"
  echo "Installed and started $LABEL."
  echo "  plist  $PLIST"
  echo "  log    $LOG"
fi

# --- verify the invariant, not the exit code ----------------------------------
echo "Waiting for the worker to answer on port $PORT (first start loads models, ~1-2 min)..."
version=""
for _ in $(seq 1 90); do
  body="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
  if [ -n "$body" ]; then
    version="$(printf '%s' "$body" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
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
