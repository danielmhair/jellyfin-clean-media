"""Central logging setup for the worker — one readable format, two sinks.

Everything the worker does should be legible from the log: which requests came
in, which jobs started, how far each pass has got, and why anything failed. One
place configures the format, level and sinks so every module — and uvicorn
itself — logs the same way.

Two sinks: stdout (which the Windows service captures to ``worker.log``, and
which you see directly when running ``scripts/worker.sh``) and a rotating file
under ``data/logs/`` so there is always a structured, size-bounded copy no
matter how the worker was launched.

Environment overrides:
  - ``CLEANMEDIA_LOG_LEVEL``  — DEBUG / INFO / WARNING / … (default DEBUG). At
    DEBUG you get every GET request and per-decile job progress; set INFO to
    quieten the routine polling.
  - ``CLEANMEDIA_LOG_FILE``   — path to the rotating log file, or empty to turn
    the file sink off (default ``data/logs/worker.log``).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional

from .store import DATA_DIR

#: Common prefix for every worker logger, so ``logging`` config can target the
#: whole app with one name and it reads clearly in the output.
ROOT_NAME = "cleanmedia"

# A wide name column keeps the message text aligned and scannable.
_FMT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False
_log_file: Optional[Path] = None


def get_logger(name: str) -> logging.Logger:
    """A logger under the shared ``cleanmedia`` root (e.g. ``cleanmedia.queue``)."""
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def configure_logging() -> Optional[Path]:
    """Install the console + rotating-file handlers on the root logger.

    Idempotent: safe to call from module import and again at startup. Returns
    the path of the rotating log file, or ``None`` if file logging is disabled.
    """
    global _configured, _log_file
    if _configured:
        return _log_file

    level_name = os.environ.get("CLEANMEDIA_LOG_LEVEL", "DEBUG").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)
    root = logging.getLogger()
    root.setLevel(level)

    # Replace any pre-existing handlers so we do not double-log if something
    # (a stray basicConfig, a re-import) already touched the root logger.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_path = _resolve_log_file()
    if file_path is not None:
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                file_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
            _log_file = file_path
        except OSError:
            # A read-only data dir must not stop the worker from booting; the
            # console sink still carries everything.
            _log_file = None

    _configured = True
    return _log_file


def _resolve_log_file() -> Optional[Path]:
    override = os.environ.get("CLEANMEDIA_LOG_FILE")
    if override is not None:
        override = override.strip()
        if override == "":
            return None  # explicitly disabled
        return Path(override)
    return DATA_DIR / "logs" / "worker.log"


def tame_uvicorn_loggers() -> None:
    """Route uvicorn's own logs through our handlers, in our format.

    Called at app startup, once uvicorn has installed its default logging. We
    drop uvicorn's private handlers and let its loggers propagate to the root,
    so startup and error lines share the worker's format and reach the file
    sink too. Its per-request access log is silenced because the worker's own
    request middleware logs richer lines (see ``main.log_requests``).
    """
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False  # our middleware replaces it
    access.disabled = True
