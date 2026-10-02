"""The worker must keep listening after clients reset their connections.

On Windows, uvicorn defaults to asyncio's Proactor loop. When a client resets
its connection before the Proactor has accepted it — routine when a laptop
wakes from Modern Standby and every request left hanging is reset at once —
Windows reports WinError 64 and asyncio closes the *listening* socket for
good. The process stays alive and never answers again. The launchers run the
selector loop instead, which logs the failed accept and keeps listening.
"""

import asyncio
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LOOP_FLAG = "--loop asyncio:SelectorEventLoop"


def test_windows_service_launcher_runs_the_selector_loop():
    script = (REPO / "scripts" / "install-service.ps1").read_text(encoding="utf-8")
    launch = [line for line in script.splitlines() if "uvicorn worker.main:app" in line]
    assert launch and all(LOOP_FLAG in line for line in launch)


def test_dev_launcher_runs_the_selector_loop_on_windows():
    script = (REPO / "scripts" / "worker.sh").read_text(encoding="utf-8")
    assert "LOOP_ARGS=(--loop asyncio:SelectorEventLoop)" in script


def _reset_storm(port: int, n: int) -> None:
    rst = struct.pack("ii", 1, 0)  # SO_LINGER 0: close() sends RST
    for _ in range(n):
        try:
            s = socket.socket()
            s.settimeout(0.3)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, rst)
            s.connect(("127.0.0.1", port))
            s.close()
        except OSError:
            pass


def _answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(b"ping")
            return s.recv(4) == b"pong"
    except OSError:
        return False


async def _survives_storm() -> bool:
    async def handle(reader, writer):
        try:
            await reader.read(4)
            writer.write(b"pong")
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    storm = threading.Thread(target=_reset_storm, args=(port, 60))
    storm.start()
    time.sleep(3)  # the loop frozen, as during sleep: resets queue up unaccepted
    storm.join()
    await asyncio.sleep(1)
    ok = await asyncio.to_thread(_answers, port)
    server.close()
    return ok


@pytest.mark.skipif(sys.platform != "win32", reason="Proactor/WinError 64 is Windows-only")
def test_selector_loop_keeps_listening_through_a_reset_storm():
    loop = asyncio.SelectorEventLoop()
    loop.set_exception_handler(lambda *_: None)  # the failed accepts are expected
    try:
        assert loop.run_until_complete(_survives_storm())
    finally:
        loop.close()
