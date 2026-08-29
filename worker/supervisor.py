"""A tiny, always-on helper that can start/restart the worker from the
Jellyfin plugin without a terminal — even when the worker process itself is
completely dead.

Why this has to be a *separate* process: the main worker cannot safely
restart itself. On Windows especially, a bare stop leaves an orphaned uvicorn
child running in the Task Scheduler task's S4U logon session, which only an
*elevated* process can kill (see scripts/install-service.ps1's
Stop-WorkerProcesses) — the worker itself runs with a Limited token and
cannot reach it. And if the worker is fully down (crashed, machine was off,
someone deleted the task), there is no worker process left to ask at all.

So this is registered as its OWN always-on service, separate from the
worker's — a real Windows Scheduled Task registered once with
``-RunLevel Highest`` (elevation authorized at *registration* time, which the
install script already required; Task Scheduler then runs every future
`schtasks /run` of it elevated with no further UAC prompt — the same
mechanism the worker's own ``-RestartCount 999`` self-heal already relies
on), or an ordinary macOS LaunchAgent (no elevation problem there at all —
launchd session isolation is a Windows-only wrinkle).

Deliberately stdlib-only (``http.server``, not FastAPI/uvicorn): this is the
last line of defence when the heavy worker process is the thing that's
broken, so it should not share its dependency graph, startup cost, or
failure modes. It always listens on 0.0.0.0 (like the worker itself) because
the whole point is being reachable from the Jellyfin plugin even when the
worker is not — binding loopback-only would defeat that.

Actions (start/restart) are gated by ``WorkerSettings.supervisorEnabled``
(worker/settings.py, editable from the plugin): disabling it does NOT stop
this process from listening — only from *acting* — so a disable can always be
undone later from the plugin. There is deliberately no separate "uninstall
yourself" action: an admin who wants this gone entirely still uses
install-service --uninstall, same as always.

No authentication, matching every other endpoint in this project: the
worker's own API already has none (see worker/main.py), and the whole
project's threat model is a trusted home LAN, not a multi-tenant service.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cleanmedia.supervisor")
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    return logger


def _worker_up(worker_port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{worker_port}/api/health", timeout=5
        ) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 — any failure means "not up"
        return False


def _supervisor_enabled() -> bool:
    """Read the plugin-editable enable flag directly from the worker's
    settings store (same process' venv, same DATA_DIR — no HTTP needed, and
    this must keep working even while the worker itself is down)."""
    try:
        from .settings import get_settings

        return get_settings().supervisorEnabled
    except Exception:  # noqa: BLE001 — a corrupt/missing store defaults open
        return True


def _set_supervisor_enabled(value: bool) -> None:
    from .settings import get_settings, set_settings

    current = get_settings()
    set_settings(current.model_copy(update={"supervisorEnabled": value}))


class MacController:
    """launchd — no elevation problem; ordinary user-session LaunchAgents."""

    def __init__(self, label: str):
        self.label = label
        self.domain = f"gui/{os.getuid()}"
        self.plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    def start(self, log: logging.Logger) -> str:
        subprocess.run(
            ["launchctl", "bootstrap", self.domain, str(self.plist)],
            capture_output=True,
        )  # harmless if already bootstrapped
        subprocess.run(["launchctl", "kickstart", "-k", f"{self.domain}/{self.label}"], check=True)
        return "started"

    def restart(self, log: logging.Logger) -> str:
        return self.start(log)  # kickstart -k restarts an already-running job too

    def stop(self, log: logging.Logger) -> str:
        subprocess.run(["launchctl", "bootout", self.domain, str(self.plist)], capture_output=True)
        return "stopped"


class WindowsController:
    """Task Scheduler — this process itself must be registered with
    -RunLevel Highest (done once, elevated, by install-service.ps1) so
    taskkill can reach the orphaned S4U child."""

    def __init__(self, task_name: str, worker_port: int):
        self.task_name = task_name
        self.worker_port = worker_port

    def _kill_orphans(self, log: logging.Logger) -> None:
        # Mirrors install-service.ps1's Stop-WorkerProcesses: end the task,
        # then hunt down whatever still holds the port (the orphaned uvicorn
        # child Task Scheduler doesn't track once the launcher .cmd exits).
        subprocess.run(["schtasks", "/end", "/tn", self.task_name], capture_output=True)
        ps = (
            "Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue "
            "| ForEach-Object { taskkill /F /T /PID $_.OwningProcess }"
        ) % self.worker_port
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
        )

    def start(self, log: logging.Logger) -> str:
        subprocess.run(["schtasks", "/run", "/tn", self.task_name], check=True, capture_output=True)
        return "started"

    def restart(self, log: logging.Logger) -> str:
        self._kill_orphans(log)
        subprocess.run(["schtasks", "/run", "/tn", self.task_name], check=True, capture_output=True)
        return "restarted"

    def stop(self, log: logging.Logger) -> str:
        self._kill_orphans(log)
        return "stopped"


class LinuxController:
    """Best-effort only (lowest priority platform for this feature): acts
    only when a systemd --user unit by this name actually exists, since this
    project otherwise leaves Linux service management to the admin's own
    setup (see scripts/worker.sh's docstring)."""

    def __init__(self, unit: str):
        self.unit = unit

    def _has_unit(self) -> bool:
        r = subprocess.run(
            ["systemctl", "--user", "status", self.unit], capture_output=True
        )
        return r.returncode != 4  # 4 == unit not found

    def start(self, log: logging.Logger) -> str:
        if not self._has_unit():
            raise RuntimeError(
                f"no systemd --user unit named {self.unit} — Linux isn't "
                "auto-configured by install-service yet, set up your own "
                "service manager (see scripts/worker.sh)"
            )
        subprocess.run(["systemctl", "--user", "start", self.unit], check=True)
        return "started"

    def restart(self, log: logging.Logger) -> str:
        if not self._has_unit():
            raise RuntimeError(f"no systemd --user unit named {self.unit}")
        subprocess.run(["systemctl", "--user", "restart", self.unit], check=True)
        return "restarted"

    def stop(self, log: logging.Logger) -> str:
        if not self._has_unit():
            raise RuntimeError(f"no systemd --user unit named {self.unit}")
        subprocess.run(["systemctl", "--user", "stop", self.unit], check=True)
        return "stopped"


def _make_controller(args: argparse.Namespace):
    system = platform.system()
    if system == "Darwin":
        return MacController(args.label)
    if system == "Windows":
        return WindowsController(args.task_name, args.worker_port)
    if system == "Linux":
        return LinuxController(args.unit)
    raise RuntimeError(f"unsupported platform: {system}")


def make_handler(args: argparse.Namespace, log: logging.Logger, controller):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _act(self, name: str, fn) -> None:
            if not _supervisor_enabled():
                self._json(403, {"error": "recovery helper is disabled from the plugin"})
                return
            try:
                result = fn(log)
                log.info("%s -> %s", name, result)
                self._json(200, {"status": result})
            except Exception as exc:  # noqa: BLE001 — report, don't crash the server
                log.exception("%s failed", name)
                self._json(500, {"error": str(exc)})

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
            if self.path == "/status":
                self._json(200, {
                    "running": _worker_up(args.worker_port),
                    "enabled": _supervisor_enabled(),
                })
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/start":
                self._act("start", controller.start)
            elif self.path == "/restart":
                self._act("restart", controller.restart)
            elif self.path == "/stop":
                # Stopping is always allowed — the enabled gate is about
                # *bringing it back*, not about being able to turn it off.
                try:
                    result = controller.stop(log)
                    log.info("stop -> %s", result)
                    self._json(200, {"status": result})
                except Exception as exc:  # noqa: BLE001
                    log.exception("stop failed")
                    self._json(500, {"error": str(exc)})
            elif self.path == "/enable":
                _set_supervisor_enabled(True)
                log.info("enabled")
                self._json(200, {"enabled": True})
            elif self.path == "/disable":
                _set_supervisor_enabled(False)
                log.info("disabled")
                self._json(200, {"enabled": False})
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, fmt: str, *fmt_args) -> None:  # noqa: A003
            log.info("%s - %s", self.address_string(), fmt % fmt_args)

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="port this helper listens on")
    parser.add_argument("--worker-port", type=int, required=True, help="the main worker's port")
    parser.add_argument("--label", default="com.cleanmedia.worker", help="macOS launchd label")
    parser.add_argument("--task-name", default="CleanMediaWorker", help="Windows Task Scheduler task name")
    parser.add_argument("--unit", default="cleanmedia-worker.service", help="Linux systemd --user unit name")
    args = parser.parse_args(argv)

    log = _setup_logging(REPO_ROOT / "data" / "logs" / "supervisor.log")
    controller = _make_controller(args)
    handler = make_handler(args, log, controller)

    log.info(
        "supervisor starting on 0.0.0.0:%d (worker port %d, platform %s)",
        args.port, args.worker_port, platform.system(),
    )
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
