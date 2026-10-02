"""The recovery helper's Windows restart must reach a worker that holds no port.

A worker whose listener has died (see tests/test_windows_listener.py) is
still alive but no longer bound to its port, so a stop that only hunts the
port's owner leaves it running — and with the task's IgnoreNew setting, the
restart's `schtasks /run` is then silently ignored.
"""

import re
from unittest import mock

from worker.supervisor import WindowsController

# Real command lines of a running Windows worker (uv launcher, the venv's
# uvicorn.exe wrapper, and the interpreter it spawns).
WORKER_CMDLINES = [
    r'"C:\Users\me\.local\bin\uv.exe" run uvicorn worker.main:app --host 0.0.0.0 --port 8765',
    r'"C:\repo\.venv\Scripts\uvicorn.exe" worker.main:app --host 0.0.0.0 --port 8765',
    r'"C:\repo\.venv\Scripts\python.exe" "C:\repo\.venv\Scripts\uvicorn.exe" worker.main:app --port 8765',
]
NOT_WORKER = [
    r'"C:\repo\.venv\Scripts\python.exe" -m worker.supervisor --port 8766 --worker-port 8765',
    r'"C:\Python312\python.exe" -m http.server 8000',
]


def _kill_script() -> str:
    calls = []
    with mock.patch("worker.supervisor.subprocess.run", side_effect=lambda a, **k: calls.append(a)):
        WindowsController("CleanMediaWorker", 8765)._kill_orphans(log=None)
    assert calls[0][:3] == ["schtasks", "/end", "/tn"]
    return calls[1][-1]


def test_kill_hunts_the_port_owner_and_worker_processes_by_command_line():
    script = _kill_script()
    assert "Get-NetTCPConnection -LocalPort 8765" in script
    assert "Win32_Process" in script and "taskkill /F /T /PID $_.ProcessId" in script


def test_command_line_pattern_matches_every_worker_process_and_nothing_else():
    pattern = re.search(r"-match '([^']+)'", _kill_script()).group(1)
    for cmd in WORKER_CMDLINES:
        assert re.search(pattern, cmd), cmd
    for cmd in NOT_WORKER:
        assert not re.search(pattern, cmd), cmd
