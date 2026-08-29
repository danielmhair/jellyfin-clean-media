"""Worker-owned settings the Jellyfin plugin's Settings/Advanced tabs edit.

Mirrors :mod:`worker.schedule` exactly: a pydantic model persisted to a JSON
file in ``DATA_DIR``, cached in memory, read fresh by whatever needs it. The
worker is the source of truth (it is the machine that actually resolves media
roots and calls the VLM), so the plugin reads and writes this through the
worker rather than storing any of it in Jellyfin's own plugin config.

Every field here is read live by its call site (``media_roots()``,
``vlm_engine._hosts()``, the per-job ``Policy``/profanity options, ...), each
falling back to an environment variable and then a hardcoded default when
unset. A save therefore takes effect on the *next* queued job — no worker
restart needed. ``mediaRoots`` is the one field with no sensible default: an
unset value means the worker is silently analyzing its own bundled test
folder instead of a real library, which the plugin surfaces as a visible
warning rather than a quiet fallback.
"""

from __future__ import annotations

import threading
from typing import Optional

from pydantic import BaseModel, Field

from .policy import DEFAULT_FIELD_GUIDANCE
from .store import DATA_DIR

_PATH = DATA_DIR / "settings.json"
_lock = threading.RLock()
_current: Optional["WorkerSettings"] = None


class WorkerSettings(BaseModel):
    """Worker-owned configuration, editable from the plugin.

    Every "" / {} / False field below means "use the fallback chain" at its
    call site (settings store -> env var -> hardcoded default) — this module
    deliberately does no fallback resolution itself, so it stays a dumb,
    trivially-testable mirror of what was actually saved.
    """

    # Required — no sensible default. An unset value means media_roots()
    # falls back to CLEANMEDIA_MEDIA_ROOTS, then the bundled movies/ folder.
    mediaRoots: str = ""

    # Ollama endpoint pool for the visual pass. "" falls back to
    # CLEANMEDIA_VLM_HOSTS, then http://localhost:11434 — that default lives
    # in vlm_engine.py, not here, so the env-var tier stays reachable.
    vlmHosts: str = ""

    # Per-observation-field overrides of the VLM's detection guidance. Only
    # keys present here (with non-empty text) override
    # policy.DEFAULT_FIELD_GUIDANCE — everything else keeps the tuned
    # default. The prompt's calibration intro and JSON-schema footer are
    # never part of this; they are fixed in worker/engines/vlm_engine.py.
    vlmGuidance: dict[str, str] = Field(default_factory=dict)

    # VLM policy toggles (see worker/policy.py's Policy dataclass — these
    # defaults intentionally match its dataclass defaults exactly).
    flagMaleShirtless: bool = False
    flagUnderwear: bool = True
    flagAnyKissing: bool = False

    # Profanity toggles (see worker/engines/profanity.py — these already
    # exist as per-job options, just never surfaced in the plugin UI before).
    profanityIncludeMild: bool = False
    profanityIncludeBlasphemy: bool = False
    profanityExtraWords: str = ""  # comma-separated

    # Whether the always-on recovery helper (see worker/supervisor.py) is
    # allowed to start/restart the worker. The helper keeps listening either
    # way — this only gates whether it acts — so disabling it from the
    # plugin can never leave it unreachable to re-enable later.
    supervisorEnabled: bool = True


class SettingsView(BaseModel):
    """The saved settings plus the real default text, for the UI to show."""

    settings: WorkerSettings
    vlmGuidanceDefault: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_FIELD_GUIDANCE)
    )


def _load_from_disk() -> WorkerSettings:
    if _PATH.exists():
        try:
            return WorkerSettings.model_validate_json(_PATH.read_text("utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt file must not break the worker
            pass
    return WorkerSettings()


def get_settings() -> WorkerSettings:
    """The current settings, cached in memory (loaded from disk once)."""
    global _current
    with _lock:
        if _current is None:
            _current = _load_from_disk()
        return _current


def set_settings(new: WorkerSettings) -> WorkerSettings:
    """Persist and cache new settings; returns the value stored."""
    global _current
    with _lock:
        DATA_DIR.mkdir(exist_ok=True)
        _PATH.write_text(new.model_dump_json(indent=2), "utf-8")
        _current = new
    return new


def view() -> SettingsView:
    return SettingsView(settings=get_settings())
