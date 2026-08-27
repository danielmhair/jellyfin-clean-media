"""Check GitHub for a newer worker release, and apply one on request.

"Update" here means: this worker's own machine pulls newer worker+plugin
source from the project's GitHub Releases (the same vX.Y.Z tags the release
workflow already cuts — see CLAUDE.md) and restarts itself. Nothing here
applies automatically — the plugin settings page shows what this module
reports and calls :func:`begin_apply` only when an administrator clicks
"Update now". A background thread just keeps the *check* fresh so that
button reflects reality without the settings page paying for a GitHub round
trip on every load.

Both the check and the apply are network calls that can fail for ordinary
reasons (no internet, GitHub hiccup, rate limit) — none of that should ever
break ``/api/health``, so every public function here degrades to a cached or
empty result instead of raising.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from . import __version__
from .logging_config import get_logger

log = get_logger("update")

GITHUB_REPO = "danielmhair/jellyfin-clean-media"
_RELEASES_LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Stay well under GitHub's 60/hr unauthenticated rate limit for this IP.
_CHECK_INTERVAL_S = 6 * 3600

# Overridable so the test suite (and, in principle, an alternate install
# layout) can point this at a throwaway directory instead of mutating the
# real checkout — same pattern as store.DATA_DIR / CLEANMEDIA_DB.
REPO_ROOT = Path(
    os.environ.get("CLEANMEDIA_REPO_ROOT") or Path(__file__).resolve().parent.parent
)

# The background checker hits the network on a timer; tests set this to "0"
# (see tests/conftest.py) so importing worker.main in the suite never does.
_CHECK_ENABLED = os.environ.get("CLEANMEDIA_UPDATE_CHECK", "1") != "0"

_lock = threading.Lock()
_state: dict = {
    "latestVersion": None,   # e.g. "0.2.17" (no leading "v")
    "releaseUrl": None,
    "notes": None,
    "checkedAt": None,       # unix time of the last check attempt
    "checkError": None,
    "updating": False,
    "applyError": None,
    "appliedAt": None,
}


class UpdateInProgress(RuntimeError):
    """Raised when begin_apply() is called while one is already running."""


class NoUpdateAvailable(RuntimeError):
    """Raised when begin_apply() is called but the worker is already current."""


def _version_tuple(v: str) -> tuple:
    v = (v or "0").lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _is_newer(candidate: str, current: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def _fetch_json(url: str, timeout: float = 10.0) -> dict:
    """Isolated network call, so tests can monkeypatch this one function
    instead of faking urllib internals."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "clean-media-worker",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_now() -> dict:
    """Ask GitHub for the latest release, refresh the cache, return status()."""
    try:
        data = _fetch_json(_RELEASES_LATEST_URL)
        tag = str(data.get("tag_name") or "").strip()
        with _lock:
            _state["latestVersion"] = tag.lstrip("vV") or None
            _state["releaseUrl"] = data.get("html_url")
            _state["notes"] = (data.get("body") or "").strip()[:2000]
            _state["checkedAt"] = time.time()
            _state["checkError"] = None
    except Exception as exc:  # noqa: BLE001 - network hiccups must not raise
        log.warning("update check failed: %s", exc)
        with _lock:
            _state["checkError"] = str(exc)
            _state["checkedAt"] = time.time()
    return status()


def status() -> dict:
    """Cached update status — no network call, safe on every /api/health."""
    with _lock:
        s = dict(_state)
    latest = s["latestVersion"]
    return {
        "currentVersion": __version__,
        "latestVersion": latest,
        "updateAvailable": bool(latest) and _is_newer(latest, __version__),
        "releaseUrl": s["releaseUrl"],
        "notes": s["notes"],
        "checkedAt": s["checkedAt"],
        "checkError": s["checkError"],
        "updating": s["updating"],
        "applyError": s["applyError"],
        "appliedAt": s["appliedAt"],
    }


def start_background_checker() -> None:
    """Refresh the cache on a timer so the settings page never blocks on
    GitHub. A no-op when CLEANMEDIA_UPDATE_CHECK=0 (the test suite)."""
    if not _CHECK_ENABLED:
        return

    def _loop() -> None:
        while True:
            check_now()
            time.sleep(_CHECK_INTERVAL_S)

    threading.Thread(target=_loop, name="update-checker", daemon=True).start()


# ---- applying an update -----------------------------------------------------


def begin_apply() -> None:
    """Start downloading + installing the latest release in the background.

    Returns as soon as it has confirmed there is something to do; the actual
    download/swap/restart happens on a worker thread — poll status() (or
    /api/health) for progress. Re-checks GitHub synchronously first so a
    click can never apply a stale cached "latest".
    """
    with _lock:
        if _state["updating"]:
            raise UpdateInProgress("an update is already in progress")
        _state["updating"] = True
        _state["applyError"] = None

    fresh = check_now()
    if not fresh["updateAvailable"]:
        with _lock:
            _state["updating"] = False
        raise NoUpdateAvailable("no update available")

    def _run() -> None:
        try:
            _apply(fresh["latestVersion"])
            with _lock:
                _state["appliedAt"] = time.time()
            log.info("update: applied %s, restarting", fresh["latestVersion"])
            _restart_service()
        except Exception as exc:  # noqa: BLE001 - reported via status(), not raised
            log.exception("update: failed")
            with _lock:
                _state["applyError"] = str(exc)
        finally:
            with _lock:
                _state["updating"] = False

    threading.Thread(target=_run, name="update-apply", daemon=True).start()


def _apply(version: str) -> None:
    tag = f"v{version}"
    zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.zip"
    with tempfile.TemporaryDirectory(prefix="cleanmedia-update-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "release.zip"
        log.info("update: downloading %s", zip_url)
        _download(zip_url, archive)

        extract_dir = tmp_path / "extracted"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)

        roots = [p for p in extract_dir.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise RuntimeError(f"unexpected release archive layout: {[p.name for p in roots]}")

        log.info("update: installing into %s", REPO_ROOT)
        _copy_into_repo(roots[0], REPO_ROOT)
        _run_setup(REPO_ROOT)


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "clean-media-worker"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _copy_into_repo(extracted_root: Path, repo_root: Path) -> None:
    """Overwrite tracked files with the new release.

    A GitHub source archive only ever contains what's tracked in git, so this
    naturally leaves untracked paths (data/, movies/, .venv/, .cleanmedia.env)
    alone without needing an explicit exclude list. It does not remove a file
    that was deleted between versions — a stray old module left on disk is
    harmless, and pruning it isn't worth the risk of an over-eager delete.
    """
    repo_root.mkdir(parents=True, exist_ok=True)
    for entry in extracted_root.iterdir():
        target = repo_root / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def _find_uv() -> Optional[str]:
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for candidate in (home / ".local" / "bin" / "uv", home / ".local" / "bin" / "uv.exe"):
        if candidate.exists():
            return str(candidate)
    return None


def _run_setup(repo_root: Path) -> None:
    uv = _find_uv()
    if uv is None:
        raise RuntimeError("uv not found on PATH; cannot install the updated dependencies")
    subprocess.run([uv, "sync"], cwd=repo_root, check=True)
    subprocess.run([uv, "run", "python", "patches/apply_patches.py"], cwd=repo_root, check=True)


def _restart_service() -> None:
    """Restart the worker via launchd, if it's running as the installed
    macOS service (scripts/install-service.sh). Anywhere else, the new code
    is on disk but a person needs to restart the worker themselves — that is
    logged, not silently skipped, so it shows up in the log an admin checks
    when "Update now" seems to hang.
    """
    plist = Path.home() / "Library" / "LaunchAgents" / "com.cleanmedia.worker.plist"
    if not plist.exists():
        log.warning(
            "update: applied on disk, but no managed service was found — "
            "restart the worker by hand to run the new code"
        )
        return
    try:
        uid = os.getuid() if hasattr(os, "getuid") else None
        target = f"gui/{uid}/com.cleanmedia.worker" if uid is not None else "com.cleanmedia.worker"
        subprocess.run(["launchctl", "kickstart", "-k", target], check=True)
    except Exception:
        log.exception(
            "update: applied on disk, but restarting the launchd service failed — "
            "restart the worker by hand"
        )
