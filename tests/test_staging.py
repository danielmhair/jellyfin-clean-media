"""Staging a flaky-share file to local disk before a long decode.

The real robocopy path is Windows-only and reads the network share, so these
tests drive the decision logic and the copy/verify contract with robocopy
stubbed — no share, no platform dependence.
"""
import os
from pathlib import Path

import pytest

from worker import staging
from worker.staging import STAGE_MIN_BYTES, local_media, should_stage


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

    def fake_robocopy(s: Path, d: Path):
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

    def truncated_robocopy(s: Path, d: Path):
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

    def boom(s: Path, d: Path):
        dirs.append(d.parent)
        raise OSError("robocopy failed (exit 8)")

    monkeypatch.setattr(staging, "_robocopy", boom)

    with pytest.raises(OSError):
        with local_media(src):
            pass
    assert dirs and not dirs[0].exists()  # no leaked 7-GB temp dir
