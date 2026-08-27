#!/usr/bin/env bash
# One-command setup for a fresh machine — the friend-friendly entry point.
#
# Goal: someone who has never touched a terminal can download this repo,
# extract it, and run ONE command to get a working worker — every dependency
# installed, the vision model pulled, and clear next steps for the plugin.
#
#   bash scripts/install.sh                 # install Ollama here + pull the model
#   bash scripts/install.sh --ollama existing --ollama-host http://192.168.1.9:11434
#   bash scripts/install.sh --yes           # accept every default, no prompts
#
# Works on macOS, Linux, and Windows *inside Git Bash* (which is why it is a
# .sh and not a .ps1 — one script, three platforms). On Windows a friend first
# installs Git Bash once (https://git-scm.com/download/win, or
# `winget install Git.Git`), then runs the line above from the repo folder.
#
# What it installs, only if missing (it never clobbers what you already have):
#   - uv        Python toolchain + the worker's dependencies
#   - ffmpeg    every engine shells out to it
#   - Ollama    the local vision model server  (skipped with --ollama existing)
#   - the vision model  (qwen3-vl:4b-instruct by default)
#
# On macOS it also offers to install the worker as a background service
# (scripts/install-service.sh) so there's no terminal window to babysit --
# see scripts/build-dmg.sh for a double-clickable .dmg wrapping this script.
#
# It deliberately does NOT install dotnet: a friend installs the Jellyfin
# plugin from the manifest URL (printed at the end), so they never build it.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

# ---- defaults & argument parsing -----------------------------------------

MODEL="qwen3-vl:4b-instruct"   # 4B is the minimum usable size (2B hallucinates)
OLLAMA_MODE=""                  # "local" | "existing"; empty => ask (or default)
OLLAMA_HOST="http://localhost:11434"
ASSUME_YES=0
PULL_MODEL=1
NEED_REOPEN=0   # set when a tool installed but isn't on THIS shell's PATH yet

usage() {
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ollama)       OLLAMA_MODE="${2:-}"; shift 2 ;;
    --ollama=*)     OLLAMA_MODE="${1#*=}"; shift ;;
    --ollama-host)  OLLAMA_HOST="${2:-}"; OLLAMA_MODE="${OLLAMA_MODE:-existing}"; shift 2 ;;
    --ollama-host=*) OLLAMA_HOST="${1#*=}"; OLLAMA_MODE="${OLLAMA_MODE:-existing}"; shift ;;
    --model)        MODEL="${2:-}"; shift 2 ;;
    --model=*)      MODEL="${1#*=}"; shift ;;
    --no-model)     PULL_MODEL=0; shift ;;
    -y|--yes)       ASSUME_YES=1; shift ;;
    -h|--help)      usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

# ---- pretty output --------------------------------------------------------

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m       %s\n' "$*"; }
info() { printf '  \033[36m..\033[0m       %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m     %s\n' "$*"; }
die()  { printf '\n\033[31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Ask a yes/no question. --yes answers "yes" to everything; a non-interactive
# shell (no tty) also takes the default so the script never hangs on a pipe.
ask_yes() { # prompt  default(y|n)
  local prompt="$1" default="${2:-n}" reply
  if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then
    [ "$default" = y ]; return
  fi
  read -r -p "  $prompt [$([ "$default" = y ] && echo Y/n || echo y/N)] " reply || true
  reply="${reply:-$default}"
  case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# ---- platform detection ---------------------------------------------------

case "$(uname -s)" in
  Darwin)                 OS=mac ;;
  Linux)                  OS=linux ;;
  MINGW*|MSYS*|CYGWIN*)   OS=windows ;;
  *) die "unsupported platform: $(uname -s)" ;;
esac
step "Clean Media setup — detected $OS"

# Refuse to run under sudo. This installer puts uv (and its Python env) under
# your HOME, and calls sudo itself only for the few system packages that need
# it. Run as root via sudo, uv lands in /root, Homebrew refuses outright, and
# you're left with a half-installed mess whose 'uv'/'ollama' your own shell
# can't find. Plain root with no sudo (a container, CI) is fine and allowed.
if [ "$(id -u 2>/dev/null || echo 1)" -eq 0 ] && [ "${SUDO_USER:-root}" != root ]; then
  die "don't run this with sudo.
       It installs into your home directory and uses sudo itself only where
       a system package needs it. Re-run it as yourself, without sudo:
           bash scripts/install.sh"
fi

# Both the vendor installers below (uv, Ollama, Homebrew) and the host probes
# need curl or wget. Fail now, with the one-liner to fix it, not halfway in.
if ! have curl && ! have wget; then
  die "need 'curl' or 'wget' to download the installers.
       macOS   : curl ships with the system (this shouldn't happen)
       Windows : reopen Git Bash — it bundles curl
       Linux   : sudo apt install curl   (or your distro's package)"
fi

# A single place to install a package by the platform's own package manager.
# Returns non-zero (without exiting) if no manager could do it, so callers can
# fall back to a static download or a clear instruction.
pkg_install() { # human-name  brew-formula  winget-id  apt-pkg
  local name="$1" brew="$2" winget="$3" apt="$4"
  case "$OS" in
    mac)
      ensure_brew || return 1
      brew list "$brew" >/dev/null 2>&1 || brew install "$brew" ;;
    windows)
      have winget || return 1
      winget install --accept-source-agreements --accept-package-agreements \
        --disable-interactivity -e --id "$winget" ;;
    linux)
      if   have apt-get; then sudo apt-get update -qq && sudo apt-get install -y "$apt"
      elif have dnf;     then sudo dnf install -y "$apt"
      elif have pacman;  then sudo pacman -Sy --noconfirm "$apt"
      elif have zypper;  then sudo zypper install -y "$apt"
      else return 1; fi ;;
  esac
}

# Homebrew is the only sane way to get ffmpeg AND ollama onto a Mac without a
# GUI download, so install it once if the friend hasn't got it. Its own
# installer pulls the Xcode command-line tools and will ask for a password.
ensure_brew() {
  have brew && return 0
  # Apple Silicon puts brew at /opt/homebrew; Intel at /usr/local — a fresh
  # install isn't on PATH yet in this shell, so add both before giving up.
  [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)" && return 0
  [ -x /usr/local/bin/brew ]    && eval "$(/usr/local/bin/brew shellenv)" && return 0
  step "Installing Homebrew (needed to install ffmpeg and Ollama on macOS)"
  info "Homebrew's installer will ask for your Mac password — that is expected."
  ask_yes "Install Homebrew now?" y || return 1
  # Only force Homebrew's own installer into non-interactive mode when WE have
  # no tty either. NONINTERACTIVE makes Homebrew check sudo access with
  # `sudo -n -v` (never prompt, fail if no cached credentials) instead of the
  # normal `sudo -v` (which prompts for the password on the tty). With a real
  # terminal attached, let it run interactively so it can actually ask for the
  # password — forcing NONINTERACTIVE here made it fail with "Need sudo
  # access... needs to be an Administrator" even for a genuine admin, because
  # it was never allowed to prompt in the first place.
  if [ -t 0 ]; then
    /bin/bash -c \
      "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  else
    NONINTERACTIVE=1 /bin/bash -c \
      "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
  [ -x /usr/local/bin/brew ]    && eval "$(/usr/local/bin/brew shellenv)"
  have brew
}

# ---- 1. uv ----------------------------------------------------------------

# A just-installed uv rarely lands on the *current* shell's PATH: the Unix
# installer drops it in ~/.local/bin, winget in its own Links dir — neither of
# which a running shell re-reads. Look everywhere it might be and add it, so the
# rest of this script (and setup.sh) can call uv in the same run.
put_uv_on_path() {
  have uv && return 0
  local c
  for c in \
    "$HOME/.local/bin/uv" "$HOME/.local/bin/uv.exe" \
    "${LOCALAPPDATA:-}/Microsoft/WinGet/Links/uv.exe" \
    "${LOCALAPPDATA:-}/Programs/uv/uv.exe" \
    "${USERPROFILE:-}/.local/bin/uv.exe"
  do
    [ -x "$c" ] && { export PATH="$(dirname "$c"):$PATH"; break; }
  done
  have uv
}

step "Python toolchain (uv)"
if have uv; then
  ok "uv already installed — $(uv --version)"
else
  info "installing uv (it also supplies Python — none needs to be preinstalled)…"
  # Windows: winget is the reliable uv installer; the Unix install.sh misbehaves
  # under MSYS. Everywhere else: the official one-liner into ~/.local/bin.
  if [ "$OS" = windows ] && have winget; then
    winget install --accept-source-agreements --accept-package-agreements \
      --disable-interactivity -e --id astral-sh.uv || true
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  if put_uv_on_path; then
    ok "installed $(uv --version)"
  else
    die "uv installed but isn't on this shell's PATH yet.
       Close this window, open a NEW Git Bash/Terminal, and run the same command
       again — it will pick up where it left off:
           bash scripts/install.sh"
  fi
fi

# ---- 2. ffmpeg ------------------------------------------------------------

step "FFmpeg (every engine needs it)"
if have ffmpeg && have ffprobe; then
  ok "ffmpeg already installed"
else
  info "installing ffmpeg…"
  if pkg_install ffmpeg ffmpeg Gyan.FFmpeg ffmpeg; then
    if have ffmpeg && have ffprobe; then
      ok "ffmpeg installed"
    else
      # Installed fine, but (typically winget on Windows) not on this shell's
      # PATH yet. ffmpeg isn't needed until you analyze a film, so don't abort —
      # just flag that a new shell is required before the worker will find it.
      warn "ffmpeg installed, but not on this shell's PATH yet — reopen your terminal before analyzing."
      NEED_REOPEN=1
    fi
  else
    die "could not install ffmpeg automatically. Install it and re-run:
       macOS   : brew install ffmpeg
       Windows : winget install Gyan.FFmpeg   (then reopen Git Bash)
       Linux   : sudo apt install ffmpeg   (or your distro's package)"
  fi
fi

# ---- 3. Ollama + the vision model ----------------------------------------

# Is an Ollama server answering at $1? (portable — curl or wget, whichever.)
ollama_up() { # base-url
  if have curl;   then curl -fsS --max-time 3 "$1/api/tags" >/dev/null 2>&1
  elif have wget; then wget -qO- --timeout=3 "$1/api/tags" >/dev/null 2>&1
  else return 1; fi
}
# Does the server at $1 already have model $2?
ollama_has_model() { # base-url  model
  local tags
  if   have curl; then tags="$(curl -fsS --max-time 5 "$1/api/tags" 2>/dev/null)" || return 1
  elif have wget; then tags="$(wget -qO- --timeout=5 "$1/api/tags" 2>/dev/null)"  || return 1
  else return 1; fi
  printf '%s' "$tags" | grep -q "\"$2\""
}
# Pull model $2 onto the server at $1. Prefer the CLI; fall back to the HTTP
# API (POST /api/pull) so the pull works even when the `ollama` binary isn't on
# this shell's PATH — the usual Windows case, where winget starts the server but
# the CLI isn't visible until a new terminal. The API streams JSON progress; we
# just wait for it and report success by re-checking the tag list.
ollama_pull() { # base-url  model
  if have ollama; then
    OLLAMA_HOST="$1" ollama pull "$2" && return 0
  fi
  if have curl; then
    info "pulling via the Ollama API (this can take a while)…"
    curl -fsS -X POST "$1/api/pull" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$2\",\"stream\":false}" >/dev/null 2>&1
  fi
  ollama_has_model "$1" "$2"   # succeeded iff the tag is now present
}

# Decide the Ollama mode if it wasn't passed on the command line.
if [ -z "$OLLAMA_MODE" ]; then
  step "Ollama (the local vision model server)"
  info "The visual pass needs Ollama. This script can install it here, or use"
  info "one you already run (on this machine or another box on your network)."
  if ask_yes "Do you already run Ollama somewhere?" n; then
    OLLAMA_MODE=existing
    if [ "$ASSUME_YES" != 1 ] && [ -t 0 ]; then
      read -r -p "  Ollama URL [$OLLAMA_HOST]: " reply || true
      OLLAMA_HOST="${reply:-$OLLAMA_HOST}"
    fi
  else
    OLLAMA_MODE=local
  fi
fi
OLLAMA_HOST="${OLLAMA_HOST%/}"   # trim any trailing slash

if [ "$OLLAMA_MODE" = existing ]; then
  step "Using your existing Ollama at $OLLAMA_HOST"
  if ollama_up "$OLLAMA_HOST"; then
    ok "reachable"
  else
    warn "cannot reach $OLLAMA_HOST right now."
    warn "make sure Ollama is running there and, if it's another machine, that it"
    warn "listens on 0.0.0.0 (OLLAMA_HOST=0.0.0.0:11434) with port 11434 open."
  fi
  if ollama_has_model "$OLLAMA_HOST" "$MODEL"; then
    ok "model $MODEL is present"
  elif [ "$PULL_MODEL" = 1 ]; then
    info "pulling $MODEL onto $OLLAMA_HOST…"
    if ollama_pull "$OLLAMA_HOST" "$MODEL"; then
      ok "model $MODEL pulled"
    else
      warn "couldn't pull it from here — run this on the Ollama machine:"
      warn "    ollama pull $MODEL"
    fi
  fi
  # Persist the remote host so the worker (CLI and plugin-triggered jobs) uses
  # it — worker.sh sources this file. See worker/engines/vlm_engine.py.
  {
    echo "# Written by scripts/install.sh — the Ollama pool the worker uses."
    echo "export CLEANMEDIA_VLM_HOSTS=\"$OLLAMA_HOST\""
  } > "$REPO/.cleanmedia.env"
  ok "saved worker config to .cleanmedia.env"

elif [ "$OLLAMA_MODE" = local ]; then
  step "Ollama (installing locally)"
  if have ollama; then
    ok "ollama already installed"
  else
    info "installing ollama…"
    case "$OS" in
      linux)   curl -fsSL https://ollama.com/install.sh | sh ;;
      mac)     pkg_install ollama ollama Ollama.Ollama ollama || true ;;
      windows) pkg_install ollama ollama Ollama.Ollama ollama || true ;;
    esac
    have ollama || die "could not install Ollama automatically.
       Download it from https://ollama.com/download , install it, then re-run."
    ok "installed ollama"
  fi

  # The model pull needs a running server. On mac/Windows the installer runs
  # Ollama as a background service already; if nothing is answering, start one.
  if ! ollama_up "$OLLAMA_HOST"; then
    info "starting the Ollama server…"
    if [ "$OS" = linux ] && have systemctl; then
      sudo systemctl enable --now ollama 2>/dev/null || nohup ollama serve >/dev/null 2>&1 &
    else
      nohup ollama serve >/dev/null 2>&1 &
    fi
    for _ in $(seq 1 20); do ollama_up "$OLLAMA_HOST" && break; sleep 1; done
  fi
  ollama_up "$OLLAMA_HOST" && ok "server is up" || warn "server not answering yet (it may still be starting)."

  if [ "$PULL_MODEL" = 1 ]; then
    if ollama_has_model "$OLLAMA_HOST" "$MODEL"; then
      ok "model $MODEL already pulled"
    else
      step "Pulling the vision model $MODEL (a few GB — one time)"
      ollama_pull "$OLLAMA_HOST" "$MODEL" \
        && ok "model $MODEL pulled" \
        || warn "pull failed — re-run: ollama pull $MODEL"
    fi
  fi
else
  die "unknown --ollama mode: '$OLLAMA_MODE' (use 'local' or 'existing')"
fi

# ---- 4. Python dependencies (reuse the existing setup step) ---------------

step "Python dependencies"
info "This is the long step: uv downloads a private Python plus PyTorch and the"
info "detection libraries — a few GB the first time. Let it run; it isn't stuck."
# setup.sh runs 'uv sync' (which provisions the managed Python and every dep)
# and re-applies the pureframe patches. Call it so the two paths never drift;
# PATH already has uv from step 1.
bash "$REPO/scripts/setup.sh"

# ---- 5. verify what actually matters --------------------------------------

step "Verifying"
FAIL=0
if have ffmpeg && have ffprobe; then
  ok "ffmpeg + ffprobe"
elif [ "$NEED_REOPEN" = 1 ]; then
  warn "ffmpeg installed but needs a fresh terminal (see the note below)"
else
  warn "ffmpeg/ffprobe missing"; FAIL=1
fi
if uv run python -c "import worker.main" >/dev/null 2>&1; then
  ok "worker imports"
else
  warn "worker failed to import — see the output above"; FAIL=1
fi
if ollama_up "$OLLAMA_HOST"; then
  ok "Ollama reachable at $OLLAMA_HOST"
  ollama_has_model "$OLLAMA_HOST" "$MODEL" && ok "model $MODEL present" \
    || warn "model $MODEL not present yet (pull it before the visual pass)"
else
  warn "Ollama not reachable at $OLLAMA_HOST (fine if it's a box you'll turn on later)"
fi

# ---- 6. run in the background (macOS only for now) ------------------------

SERVICE_INSTALLED=0
if [ "$OS" = mac ]; then
  step "Run automatically in the background"
  info "Without this, the worker only runs while you keep a Terminal window open."
  if ask_yes "Set the worker to start automatically and keep running (recommended)?" y; then
    if bash "$REPO/scripts/install-service.sh"; then
      SERVICE_INSTALLED=1
    else
      warn "could not install the background service — you can run it later:"
      warn "    bash scripts/install-service.sh"
    fi
  fi
fi

# ---- 7. next steps ----------------------------------------------------------

MANIFEST="https://raw.githubusercontent.com/danielmhair/jellyfin-clean-media/main/manifest.json"
step "Done${FAIL:+ (with warnings above)}"
if [ "$NEED_REOPEN" = 1 ]; then
  cat <<EOF

  NOTE: something (usually ffmpeg on Windows) was just installed but isn't on
  this window's PATH yet. Close this terminal and open a NEW one before starting
  the worker, so it can find everything.
EOF
fi
if [ "$SERVICE_INSTALLED" = 1 ]; then
  cat <<EOF

  You're set up. The worker is already running in the background (it will
  restart itself at login, and after any future update). Two things left:

  1. Install the plugin in Jellyfin — no building needed:
       Dashboard -> Plugins -> Repositories -> add this URL:
         $MANIFEST
       then install "Clean Media" from the catalogue and restart Jellyfin.

  2. In Dashboard -> Plugins -> Clean Media, set the Worker URL to this
     machine (e.g. http://<this-machine-ip>:8765) and click Test connection.

  Then analyze a film:
       bash scripts/analyze-audio.sh movies/                  # profanity, ~1 min each
       bash scripts/analyze.sh "movies/Some Film (2010).mkv"  # + visual (GPU, hours)

EOF
else
  cat <<EOF

  You're set up. Three things left, none of them require a terminal again:

  1. Start the worker (leave this running whenever you want to analyze):
         bash scripts/worker.sh
EOF
  [ "$OS" = mac ] && cat <<EOF
       Or run it in the background instead, so you don't have to keep a
       window open: bash scripts/install-service.sh
EOF
  cat <<EOF

  2. Install the plugin in Jellyfin — no building needed:
       Dashboard -> Plugins -> Repositories -> add this URL:
         $MANIFEST
       then install "Clean Media" from the catalogue and restart Jellyfin.

  3. In Dashboard -> Plugins -> Clean Media, set the Worker URL to this
     machine (e.g. http://<this-machine-ip>:8765) and click Test connection.

  Then analyze a film:
       bash scripts/analyze-audio.sh movies/                  # profanity, ~1 min each
       bash scripts/analyze.sh "movies/Some Film (2010).mkv"  # + visual (GPU, hours)

EOF
fi
