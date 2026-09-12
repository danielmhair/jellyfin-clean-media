"""Staging a flaky-share file to local disk before a long decode.

The real robocopy path is Windows-only and reads the network share, so these
tests drive the decision logic and the copy/verify contract with robocopy
stubbed — no share, no platform dependence.
"""
import os
from pathlib import Path

import pytest

from worker import staging
from worker.staging import STAGE_MIN_BYTES, local_media, local_output, should_stage


def _make_file(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def test_local_path_is_never_staged(tmp_path):
    f = _make_file(tmp_path / "movie.mkv", 4096)
    assert should_stage(f) is False
    with local_media(f) as p:
        assert p == f  # yielded unchanged, no copy


def test_unc_detection_and_size_gate(tmp_path, monkeypatch):
    # Force the UNC branch and a present robocopy regardless of path/platform,
    # so the size gate is what's under test here (not the environment).
    monkeypatch.setattr(staging.os, "name", "nt")
    monkeypatch.setattr(staging, "_is_unc", lambda p: True)
    monkeypatch.setattr(staging.shutil, "which", lambda name: "robocopy")

    small = _make_file(tmp_path / "small.mkv", 1024)
    big = _make_file(tmp_path / "big.mkv", STAGE_MIN_BYTES + 1)
    assert should_stage(small) is False  # below threshold -> retry path handles it
    assert should_stage(big) is True


def test_stages_and_yields_local_copy_then_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "should_stage", lambda p: True)

    src = _make_file(tmp_path / "src" / "movie.mkv", 2048)

    captured = {}

    def fake_robocopy(s: Path, d: Path, on_percent=None):
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(s.read_bytes())  # a complete, byte-exact copy
        captured["staging_dir"] = d.parent

    monkeypatch.setattr(staging, "_robocopy", fake_robocopy)

    with local_media(src) as p:
        assert p != src
        assert p.read_bytes() == src.read_bytes()
        assert p.exists()
    # temp staging dir is removed on exit
    assert not captured["staging_dir"].exists()


def test_short_copy_is_rejected_not_used(tmp_path, monkeypatch):
    """A copy that stops short (a drop robocopy could not finish) must raise,

    never silently yield a truncated file that would mistranscribe the film.
    """
    monkeypatch.setattr(staging, "should_stage", lambda p: True)
    src = _make_file(tmp_path / "src" / "movie.mkv", 4096)

    def truncated_robocopy(s: Path, d: Path, on_percent=None):
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(s.read_bytes()[:100])  # short

    monkeypatch.setattr(staging, "_robocopy", truncated_robocopy)

    with pytest.raises(OSError, match="stopped short"):
        with local_media(src) as p:  # noqa: F841
            pass


def test_staging_dir_cleaned_up_even_when_robocopy_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "should_stage", lambda p: True)
    src = _make_file(tmp_path / "src" / "movie.mkv", 4096)
    dirs = []

    def boom(s: Path, d: Path, on_percent=None):
        dirs.append(d.parent)
        raise OSError("robocopy failed (exit 8)")

    monkeypatch.setattr(staging, "_robocopy", boom)

    with pytest.raises(OSError):
        with local_media(src):
            pass
    assert dirs and not dirs[0].exists()  # no leaked 7-GB temp dir


def test_output_local_path_is_never_staged(tmp_path):
    dest = tmp_path / "Some Film - Clean.mkv"
    with local_output(dest) as p:
        assert p == dest  # yielded unchanged, ffmpeg writes straight there


def test_output_stages_locally_then_copies_to_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "_is_unc", lambda p: True)
    monkeypatch.setattr(staging.shutil, "which", lambda name: "robocopy")

    dest = tmp_path / "network" / "Some Film - Clean.mkv"
    captured = {}

    def fake_robocopy(s: Path, d: Path, on_percent=None):
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(s.read_bytes())
        captured["staging_dir"] = s.parent

    monkeypatch.setattr(staging, "_robocopy", fake_robocopy)

    with local_output(dest) as local_path:
        assert local_path != dest
        assert not dest.exists()  # nothing on the network yet
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"rendered bytes")

    assert dest.read_bytes() == b"rendered bytes"
    assert not captured["staging_dir"].exists()  # local temp cleaned up


def test_output_short_copy_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "_is_unc", lambda p: True)
    monkeypatch.setattr(staging.shutil, "which", lambda name: "robocopy")

    dest = tmp_path / "network" / "Some Film - Clean.mkv"

    def truncated_robocopy(s: Path, d: Path, on_percent=None):
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(s.read_bytes()[:1])

    monkeypatch.setattr(staging, "_robocopy", truncated_robocopy)

    with pytest.raises(OSError, match="stopped short"):
        with local_output(dest) as local_path:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"rendered bytes")


def test_output_never_touches_destination_when_render_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "_is_unc", lambda p: True)
    monkeypatch.setattr(staging.shutil, "which", lambda name: "robocopy")

    dest = tmp_path / "network" / "Some Film - Clean.mkv"

    def must_not_run(s: Path, d: Path, on_percent=None):
        raise AssertionError("robocopy must not run when the render itself failed")

    monkeypatch.setattr(staging, "_robocopy", must_not_run)

    with pytest.raises(RuntimeError, match="ffmpeg exploded"):
        with local_output(dest) as local_path:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"partial")
            raise RuntimeError("ffmpeg exploded")

    assert not dest.exists()  # never created on the network


def test_local_media_progress_tracks_copy_percentage(tmp_path, monkeypatch):
    """The caller's progress bar should move during the copy, not sit at 0%."""
    monkeypatch.setattr(staging, "should_stage", lambda p: True)
    src = _make_file(tmp_path / "src" / "movie.mkv", 4096)

    def fake_robocopy(s: Path, d: Path, on_percent=None):
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(s.read_bytes())
        for pct in (0.0, 45.0, 100.0):
            on_percent(pct)

    monkeypatch.setattr(staging, "_robocopy", fake_robocopy)

    seen = []
    with local_media(src, lambda frac, stage: seen.append((frac, stage))):
        pass

    # The initial call, then one per reported percentage, each below 1.0 and
    # naming the running percent so the message actually changes over time.
    assert seen[0] == (0.0, "staging movie.mkv to local disk (0.0 GB)")
    assert [round(f, 2) for f, _ in seen[1:]] == [0.0, 0.45, 0.99]
    assert "45%" in seen[2][1]


def test_output_progress_percent_never_regresses_the_fraction(tmp_path, monkeypatch):
    """The network copy is the last step of an hours-long render — its own

    0-100% must not make the job's overall progress bar jump backward from
    wherever ffmpeg left it.
    """
    monkeypatch.setattr(staging, "_is_unc", lambda p: True)
    monkeypatch.setattr(staging.shutil, "which", lambda name: "robocopy")
    dest = tmp_path / "network" / "movie - Clean.mkv"

    def fake_robocopy(s: Path, d: Path, on_percent=None):
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(s.read_bytes())
        for pct in (10.0, 60.0):
            on_percent(pct)

    monkeypatch.setattr(staging, "_robocopy", fake_robocopy)

    seen = []
    with local_output(dest, lambda frac, stage: seen.append((frac, stage))) as p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"rendered")

    assert all(frac == 0.99 for frac, _ in seen)
    assert any("60%" in stage for _, stage in seen)


def test_robocopy_parses_percent_from_carriage_returned_output(monkeypatch):
    """robocopy rewrites its live percentage with a bare \\r, never \\n — the

    parser must split on either, not just newlines.
    """

    class FakeStdout:
        def __init__(self, text: str) -> None:
            self._chars = iter(text)

        def read(self, n: int) -> str:
            assert n == 1
            return next(self._chars, "")

    class FakeProc:
        def __init__(self, text: str) -> None:
            self.stdout = FakeStdout(text)
            self.returncode = 1  # robocopy: 1 = files copied, success

        def wait(self) -> None:
            pass

    fake_output = "\t    New File  \t\t4096\tmovie.mkv\r  0%\r 45%\r100%\r"
    monkeypatch.setattr(
        staging.subprocess, "Popen", lambda *a, **k: FakeProc(fake_output)
    )

    seen = []
    staging._robocopy(Path("src/movie.mkv"), Path("dst/movie.mkv"), seen.append)
    assert seen == [0.0, 45.0, 100.0]
