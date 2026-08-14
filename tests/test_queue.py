"""The persisted-order job queue: reorder, requeue, pause, and run order.

These drive the real :class:`JobQueue` (the same injectable harness
``tests/test_recovery.py`` uses) and assert on observable behaviour — the order
the recording engine actually runs jobs in, and the persisted job rows — never
on private fields. The one structural change this feature makes (FIFO →
persisted ``queuePosition``) lives here, so this is where it is pinned down.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from worker.engines.base import EngineAdapter
from worker.models import Job, JobCreate, JobStatus, Timeline
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


def _media(tmp_path, i):
    """A distinct throwaway media file (distinct bytes → distinct fingerprint)."""
    media = tmp_path / f"Film {i}.mkv"
    media.write_bytes(b"x" * 4096 + str(i).encode())
    return media


def _save(store, tmp_path, i, status=JobStatus.queued, engine="fake",
          position=None, error=None, progress=0.0):
    """Write a job row straight to the store, as a prior run would have left it."""
    media = _media(tmp_path, i)
    job = Job(
        id=f"job{i}",
        mediaPath=str(media),
        engine=engine,
        mediaFingerprint=f"fp{i}",
        status=status,
        progress=progress,
        error=error,
        queuePosition=position,
        createdAt=BASE + timedelta(seconds=i),
    )
    store.save_job(job)
    return job


# -- submit assigns positions -------------------------------------------------


def test_submit_assigns_increasing_positions(engine, tmp_path):
    # Window closed, so nothing runs and the positions can be inspected at rest.
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: False, poll_s=0.01)

    ids = [
        q.submit(JobCreate(mediaPath=str(_media(tmp_path, i)), engine="fake")).id
        for i in range(3)
    ]

    positions = [store.get_job(jid).queuePosition for jid in ids]
    assert positions == [0, 1, 2]  # each submission goes to the back


# -- reorder ------------------------------------------------------------------


def test_reorder_changes_run_order(engine, tmp_path):
    """Queue several jobs outside the window, reorder, open the window — the
    engine runs them in the NEW order, because the worker never commits to a job
    while it waits."""
    gate = {"open": False}
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: gate["open"], poll_s=0.01)

    a = q.submit(JobCreate(mediaPath=str(_media(tmp_path, 0)), engine="fake"))
    b = q.submit(JobCreate(mediaPath=str(_media(tmp_path, 1)), engine="fake"))
    c = q.submit(JobCreate(mediaPath=str(_media(tmp_path, 2)), engine="fake"))
    # Nothing may run yet.
    time.sleep(0.1)
    assert engine.order == []

    q.reorder([c.id, a.id, b.id])
    gate["open"] = True

    assert _wait(lambda: len(engine.order) == 3)
    assert engine.order == ["Film 2.mkv", "Film 0.mkv", "Film 1.mkv"]


def test_reorder_ignores_running_terminal_and_unknown_ids(engine, tmp_path):
    """Reorder repositions only queued/rendering jobs; a running, terminal or
    unknown id is left exactly where it was."""
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: False, poll_s=0.01)

    _save(store, tmp_path, 1, status=JobStatus.queued, position=0)
    _save(store, tmp_path, 2, status=JobStatus.running, position=5)
    _save(store, tmp_path, 3, status=JobStatus.completed, position=9)

    q.reorder(["job3", "job2", "nope", "job1"])

    assert store.get_job("job1").queuePosition == 0  # only pending id repositioned
    assert store.get_job("job2").queuePosition == 5  # running untouched
    assert store.get_job("job3").queuePosition == 9  # terminal untouched


# -- requeue ------------------------------------------------------------------


def test_requeue_failed_job_reruns_with_error_cleared(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    _save(store, tmp_path, 0, status=JobStatus.failed, error="no subtitle track")
    q = JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)

    job = q.requeue("job0")
    assert job.status == JobStatus.queued
    assert job.error is None

    assert _wait(lambda: store.get_job("job0").status == JobStatus.completed)
    assert engine.calls == 1  # it actually re-ran


def test_requeue_sends_the_job_to_the_back(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    _save(store, tmp_path, 0, status=JobStatus.queued, position=0)  # already waiting
    _save(store, tmp_path, 1, status=JobStatus.failed, position=1)
    q = JobQueue(store, allowed_fn=lambda now: False, poll_s=0.01)

    job = q.requeue("job1")
    # Fresh end-of-queue position: above every position now in the store.
    assert job.queuePosition > store.get_job("job0").queuePosition


def test_requeue_is_refused_for_running_or_queued(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: False, poll_s=0.01)
    # Saved AFTER construction so recovery does not reset the running one.
    _save(store, tmp_path, 0, status=JobStatus.queued)
    _save(store, tmp_path, 1, status=JobStatus.running)

    with pytest.raises(ValueError):
        q.requeue("job0")
    with pytest.raises(ValueError):
        q.requeue("job1")
    with pytest.raises(KeyError):
        q.requeue("missing")


# -- pause / resume -----------------------------------------------------------


def test_pause_holds_the_next_start_and_resume_releases_it(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)
    q.set_paused(True)

    q.submit(JobCreate(mediaPath=str(_media(tmp_path, 0)), engine="fake"))
    time.sleep(0.2)
    assert engine.order == []  # paused: nothing starts

    q.set_paused(False)
    assert _wait(lambda: engine.order == ["Film 0.mkv"])  # resume releases it
