"""End-to-end harness for the review "Studio" page.

Boots a *real* worker (uvicorn subprocess) against a *synthesized* media file
and drives the actual page in a browser — no mocking, no gitignored movie. Each
test asserts on the observable outcome, above all what lands in the
`.cleanmedia.json` sidecar, which is the source of truth the plugin reads.

The suite skips itself cleanly when Playwright's browser isn't installed, so a
plain ``uv run pytest`` stays green for anyone. To run it for real:

    uv sync
    uv run playwright install chromium
    uv run pytest tests/e2e
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

# No browser installed → skip the whole directory rather than error.
pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MEDIA_NAME = "Some Film (2010).mkv"

# The canonical findings the page starts each test from: a visual finding with a
# render-only action (gore/blur), a visual skip (nudity), two adjacent profanity
# words (for merge), and a tentative one — one of each kind the UI treats
# differently. Reset before every test so tests never leak into each other.
CANON_SEGMENTS = [
    {"id": 1, "startMs": 8000, "endMs": 11000, "category": "gore", "confidence": 0.83,
     "engine": "vlm", "recommendedAction": "blur", "approved": None,
     "reasoning": "blood/wound detail across 5 shots; policy: gore", "engineRef": "shot-12"},
    {"id": 2, "startMs": 20000, "endMs": 24000, "category": "nudity", "confidence": 0.98,
     "engine": "vlm", "recommendedAction": "skip", "approved": None,
     "reasoning": "female_topless=true across 6 shots; policy: nudity", "engineRef": "shot-30"},
    {"id": 3, "startMs": 40000, "endMs": 40900, "category": "profanity", "confidence": 1.0,
     "engine": "subtitles", "recommendedAction": "mute", "approved": None,
     "reasoning": "[damn] (single-word-cue)", "engineRef": "cue-3"},
    # A second "damn" so one type has >1 finding — lets a test filter to it and
    # bulk-cut the whole group.
    {"id": 4, "startMs": 41500, "endMs": 42200, "category": "profanity", "confidence": 0.9,
     "engine": "subtitles", "recommendedAction": "mute", "approved": None,
     "reasoning": "[damn] (cached-asr)", "engineRef": "cue-4"},
    {"id": 5, "startMs": 60000, "endMs": 66000, "category": "suggestive", "confidence": 0.6,
     "engine": "vlm", "recommendedAction": "skip", "approved": None,
     "reasoning": "partial undress, no explicit nudity — raised for your call",
     "engineRef": "shot-70"},
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_sidecar(media: Path, segments) -> Path:
    sidecar = media.with_name(media.stem + ".cleanmedia.json")
    sidecar.write_text(
        json.dumps({
            "schemaVersion": 1,
            "mediaFingerprint": "e2e-fixture",
            "nextSegmentId": 100,
            "segments": segments,
        }, indent=2),
        encoding="utf-8",
    )
    return sidecar


@pytest.fixture(scope="session")
def worker(tmp_path_factory):
    """A real worker serving a real, synthesized film. Session-scoped: the
    ffmpeg encode and process start are the expensive parts, and the sidecar is
    reset per test (below) rather than restarting the server."""
    media_dir = tmp_path_factory.mktemp("e2e-media")
    media = media_dir / MEDIA_NAME

    # A short deterministic MKV: test pattern + tone. Exercises the real
    # ffmpeg-backed peaks/filmstrip/thumbnail paths, not stubs.
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=90",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=90",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(media)],
        check=True, capture_output=True,
    )
    _write_sidecar(media, CANON_SEGMENTS)

    port = _free_port()
    env = {
        **os.environ,
        "CLEANMEDIA_MEDIA_ROOTS": str(media_dir),
        "CLEANMEDIA_DB": str(media_dir / "jobs.db"),
        "CLEANMEDIA_LOG_FILE": "",
    }
    # Redirect the worker's output to a file, never a PIPE: an undrained pipe
    # fills its ~64KB OS buffer after a few dozen requests, the worker blocks on
    # its next write, and every later request hangs. A file has no such limit.
    log_path = media_dir / "worker.out"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "worker.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=env,
        stdout=log_fh, stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{port}"
    review_url = base + "/api/review?path=" + urllib.parse.quote(MEDIA_NAME)
    deadline = time.time() + 45
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            out = log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"worker exited early:\n{out}")
        try:
            with urllib.request.urlopen(review_url, timeout=2) as r:
                if r.status == 200:
                    ready = True
                    break
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.4)
    if not ready:
        proc.terminate()
        raise RuntimeError("worker did not become ready in time")

    yield {"base": base, "review_url": review_url, "media": media,
           "sidecar": media.with_name(media.stem + ".cleanmedia.json")}

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    finally:
        log_fh.close()


@pytest.fixture()
def sidecar(worker):
    """Reset the film to the canonical findings before each test, and hand back
    a reader so a test can assert on exactly what persisted."""
    _write_sidecar(worker["media"], CANON_SEGMENTS)

    class Sidecar:
        path = worker["sidecar"]

        def read(self):
            return json.loads(self.path.read_text(encoding="utf-8"))["segments"]

        def by_id(self, sid):
            return next((s for s in self.read() if s["id"] == sid), None)

        def by_category(self, cat):
            return sorted((s for s in self.read() if s["category"] == cat),
                          key=lambda s: s["startMs"])

    return Sidecar()


@pytest.fixture(scope="session")
def _browser():
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # browser binary missing / cannot launch
            pytest.skip(f"chromium not launchable ({exc}); "
                        "run `uv run playwright install chromium`")
        yield browser
        browser.close()


@pytest.fixture()
def page(worker, sidecar, _browser):
    """A fresh page already loaded on the review Studio for the fixture film."""
    context = _browser.new_context(viewport={"width": 1400, "height": 900})
    page = context.new_page()
    page.goto(worker["review_url"])
    # The page hydrates from embedded JSON synchronously; wait for the rail.
    page.wait_for_selector("#D-list .drow")
    yield page
    context.close()
