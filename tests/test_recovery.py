"""Crash/restart recovery: the queue survives a worker restart.

The processing queue lives in memory, so a restart empties it while the job
rows persist in the store. On startup the queue must re-enqueue everything left
unfinished, in the original submission order, and re-run whatever was mid-flight
— so a reboot resumes the batch instead of dropping it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from worker.engines.base import EngineAdapter
from worker.models import Job, JobStatus, Timeline
from worker.queue import JobQueue
from worker.store import Store

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _RecordingEngine(EngineAdapter):
    """Records the order films are analysed in, so a test can assert on it."""

    name = "fake"
    resumable = False

    def __init__(self) -> None:
        self.order: list[str] = []
        self.calls = 0

    def version(self) -> str:
        return "1.0"

    def health(self):
        return {}

    def capabilities(self):
        return {}

    def analyze(self, media_path, fingerprint, options, progress):
        self.calls += 1
        self.order.append(Path(media_path).name)
        progress(1.0, "done")
        return Timeline(mediaFingerprint=fingerprint, segments=[]), None

    def render(self, *args, **kwargs):
        raise NotImplementedError


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def engine(monkeypatch):
    import worker.queue as queue_mod

    eng = _RecordingEngine()
    monkeypatch.setitem(queue_mod.ENGINES, "fake", eng)
    return eng


def _persist_job(store, tmp_path, i, status=JobStatus.queued, engine="fake", progress=0.0):
    """Write a job row straight to the store, as a prior run would have left it."""
    media = tmp_path / f"Film {i}.mkv"
    media.write_bytes(b"x" * 4096)
    job = Job(
        id=f"job{i}",
        mediaPath=str(media),
        engine=engine,
        mediaFingerprint=f"fp{i}",
        status=status,
        progress=progress,
        createdAt=BASE + timedelta(seconds=i),
    )
    store.save_job(job)
    return job


# -- ordering & resume --------------------------------------------------------


def test_recovers_queued_jobs_in_submission_order(engine, tmp_path):
    # Three jobs a prior run had queued but never processed, saved out of order.
    store = Store(db_path=tmp_path / "jobs.db")
    for i in (2, 0, 1):
        _persist_job(store, tmp_path, i)

    JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)  # recovery runs in __init__

    assert _wait(lambda: len(engine.order) == 3)
    # Oldest createdAt first, regardless of the order they were written.
    assert engine.order == ["Film 0.mkv", "Film 1.mkv", "Film 2.mkv"]


def test_interrupted_running_job_is_reset_and_rerun(engine, tmp_path):
    # A pass that was mid-run (status=running, half done) when the worker died.
    store = Store(db_path=tmp_path / "jobs.db")
    _persist_job(store, tmp_path, 0, status=JobStatus.running, progress=0.5)

    JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)

    assert _wait(lambda: store.get_job("job0").status == JobStatus.completed)
    assert engine.calls == 1  # it was re-run, not left stuck at 50%


def test_terminal_jobs_are_not_recovered(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    _persist_job(store, tmp_path, 0, status=JobStatus.completed)
    _persist_job(store, tmp_path, 1, status=JobStatus.failed)
    _persist_job(store, tmp_path, 2, status=JobStatus.cancelled)
    _persist_job(store, tmp_path, 3, status=JobStatus.rendered)

    JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)

    # Nothing should be picked up; give the worker a moment to prove it.
    time.sleep(0.3)
    assert engine.calls == 0


def test_empty_store_recovers_nothing(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)
    time.sleep(0.2)
    assert engine.calls == 0


# -- kind classification ------------------------------------------------------


@pytest.mark.parametrize(
    "status,engine_name,expected",
    [
        (JobStatus.queued, "whisper", "analyze"),
        (JobStatus.running, "whisper", "analyze"),
        (JobStatus.rendering, "render", "render_media"),
        (JobStatus.rendering, "whisper", "render"),
        (JobStatus.completed, "whisper", None),
        (JobStatus.failed, "whisper", None),
        (JobStatus.cancelled, "whisper", None),
        (JobStatus.rendered, "render", None),
    ],
)
def test_kind_for_classifies_persisted_jobs(status, engine_name, expected):
    job = Job(id="x", mediaPath="/m/Some Film (2010).mkv", engine=engine_name, status=status)
    assert JobQueue._kind_for(job) == expected
