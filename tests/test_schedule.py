"""The analysis schedule: the allowed-window logic and the queue's gating.

Two things are covered: ``is_allowed`` (per-weekday windows, including ones
that wrap past midnight), and the job queue actually honouring the schedule —
holding a pass before it starts and pausing a resumable pass mid-run, then
resuming when the window reopens.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from worker.engines.base import EngineAdapter
from worker.models import JobCreate, JobStatus, Timeline
from worker.queue import JobQueue
from worker.schedule import DayWindow, Schedule, _normalize, is_allowed
from worker.store import Store

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def _at(weekday: int, hour: int, minute: int = 0) -> datetime:
    """A datetime whose weekday()/hour/minute are what the schedule reads."""
    # 2026-08-10 is a Monday, so adding `weekday` days lands on that weekday.
    return datetime(2026, 8, 10 + weekday, hour, minute)


def _day(start_h, end_h, enabled=True) -> DayWindow:
    return DayWindow(enabled=enabled, start=start_h * 60, end=end_h * 60)


# -- is_allowed ---------------------------------------------------------------


def test_disabled_schedule_always_allows():
    sched = Schedule(enabled=False, days=[_day(2, 7) for _ in range(7)])
    assert is_allowed(_at(MON, 12), sched) is True
    assert is_allowed(_at(SAT, 3), sched) is True


def test_same_day_window():
    sched = Schedule(enabled=True, days=[_day(2, 7) for _ in range(7)])
    assert is_allowed(_at(MON, 1, 59), sched) is False
    assert is_allowed(_at(MON, 2, 0), sched) is True
    assert is_allowed(_at(MON, 6, 59), sched) is True
    assert is_allowed(_at(MON, 7, 0), sched) is False  # end is exclusive


def test_window_wrapping_past_midnight():
    # 23:00 Monday through 07:00 Tuesday.
    days = [DayWindow() for _ in range(7)]
    days[MON] = _day(23, 7)  # start(23:00) > end(07:00) => wraps
    sched = Schedule(enabled=True, days=days)
    assert is_allowed(_at(MON, 23, 30), sched) is True   # evening, this day
    assert is_allowed(_at(MON, 22, 59), sched) is False
    assert is_allowed(_at(TUE, 6, 59), sched) is True    # morning spill next day
    assert is_allowed(_at(TUE, 7, 0), sched) is False
    assert is_allowed(_at(TUE, 12, 0), sched) is False    # Tuesday itself is off


def test_all_day_when_start_equals_end():
    days = [DayWindow() for _ in range(7)]
    days[SAT] = DayWindow(enabled=True, start=0, end=0)  # all Saturday
    sched = Schedule(enabled=True, days=days)
    assert is_allowed(_at(SAT, 0, 0), sched) is True
    assert is_allowed(_at(SAT, 13, 37), sched) is True
    assert is_allowed(_at(SAT, 23, 59), sched) is True
    assert is_allowed(_at(FRI, 13, 0), sched) is False    # neighbouring day off


def test_disabled_day_blocks():
    days = [_day(0, 24) for _ in range(7)]  # 00:00..24:00 every day
    days[WED] = DayWindow(enabled=False)
    sched = Schedule(enabled=True, days=days)
    assert is_allowed(_at(TUE, 12), sched) is True
    assert is_allowed(_at(WED, 12), sched) is False


# -- timezone -----------------------------------------------------------------


def test_aware_now_is_evaluated_in_the_schedule_timezone():
    # 02:00–07:00 nightly, windows expressed in America/Denver (UTC-6 in Aug).
    sched = Schedule(enabled=True, days=[_day(2, 7) for _ in range(7)], tz="America/Denver")
    # 08:30 UTC on a Monday == 02:30 Denver — inside the window.
    inside = datetime(2026, 8, 10, 8, 30, tzinfo=timezone.utc)
    assert is_allowed(inside, sched) is True
    # 08:30 UTC is 02:30 Denver but that instant is still Sunday-night's window
    # rolling into Monday morning; a mid-afternoon UTC instant is daytime Denver.
    daytime = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)  # 14:00 Denver
    assert is_allowed(daytime, sched) is False


def test_naive_now_is_treated_as_local_wall_clock_even_with_tz():
    # A naive datetime (tests / legacy callers) is read verbatim, so the tz field
    # doesn't retroactively shift the existing per-weekday assertions.
    sched = Schedule(enabled=True, days=[_day(2, 7) for _ in range(7)], tz="America/Denver")
    assert is_allowed(_at(MON, 3), sched) is True
    assert is_allowed(_at(MON, 12), sched) is False


def test_bad_timezone_is_dropped_on_normalize():
    sched = _normalize(Schedule(enabled=True, days=[_day(2, 7)], tz="Not/AZone"))
    assert sched.tz == ""  # falls back to worker-local rather than erroring


def test_valid_timezone_survives_normalize():
    sched = _normalize(Schedule(enabled=True, days=[_day(2, 7)], tz="America/Denver"))
    assert sched.tz == "America/Denver"


# -- queue gating -------------------------------------------------------------


class _FakeEngine(EngineAdapter):
    """A resumable engine that emits progress over ~0.5s so a test can flip the
    window mid-run. It restarts from the top each call (the real VLM engine
    resumes from its checkpoint file); the queue's job is only to re-queue."""

    name = "fake"
    resumable = True

    def __init__(self) -> None:
        self.calls = 0

    def version(self) -> str:
        return "1.0"

    def health(self):
        return {}

    def capabilities(self):
        return {}

    def analyze(self, media_path, fingerprint, options, progress):
        self.calls += 1
        for i in range(1, 201):
            progress(i / 200.0, f"step {i}")
            time.sleep(0.01)  # ~2s total, so a test can flip the window mid-run
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
def gated(tmp_path, monkeypatch):
    """A queue wired to a controllable allow-flag and a fake resumable engine."""
    import worker.queue as queue_mod

    engine = _FakeEngine()
    monkeypatch.setitem(queue_mod.ENGINES, "fake", engine)

    allow = {"ok": True}
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(
        store,
        allowed_fn=lambda now: allow["ok"],
        poll_s=0.01,
    )
    media = tmp_path / "Film.mkv"
    media.write_bytes(b"pretend video with a real size on disk")
    return q, store, engine, allow, media


def _status(store, job_id):
    job = store.get_job(job_id)
    return job.status if job else None


def test_job_held_before_start_until_window_opens(gated):
    q, store, engine, allow, media = gated
    allow["ok"] = False  # closed from the outset

    job = q.submit(JobCreate(mediaPath=str(media), engine="fake"))

    assert _wait(lambda: (store.get_job(job.id).stage == "waiting for scheduled hours"))
    assert _status(store, job.id) == JobStatus.queued
    assert engine.calls == 0  # never started outside the window

    allow["ok"] = True
    assert _wait(lambda: _status(store, job.id) == JobStatus.completed)
    assert engine.calls == 1


def test_running_job_pauses_and_resumes_when_window_closes(gated):
    q, store, engine, allow, media = gated

    job = q.submit(JobCreate(mediaPath=str(media), engine="fake"))
    assert _wait(lambda: _status(store, job.id) == JobStatus.running)

    # Close the window mid-run: the next progress tick raises, the job is
    # re-queued (back to queued, not failed) and then waits for the window. The
    # brief "paused" stage is quickly replaced by "waiting for scheduled hours"
    # as it re-enters the gate, so assert on the stable queued state.
    allow["ok"] = False
    assert _wait(lambda: _status(store, job.id) == JobStatus.queued)
    assert engine.calls >= 1
    # It stays parked (not failed, not completed) while the window is shut.
    time.sleep(0.2)
    assert _status(store, job.id) == JobStatus.queued

    # Reopen: it resumes (re-runs) and finishes.
    allow["ok"] = True
    assert _wait(lambda: _status(store, job.id) == JobStatus.completed)
    assert engine.calls >= 2  # it was started again after the pause


def test_non_resumable_job_is_not_paused_midrun(gated, monkeypatch):
    q, store, engine, allow, media = gated
    engine.resumable = False  # e.g. a short profanity pass

    job = q.submit(JobCreate(mediaPath=str(media), engine="fake"))
    assert _wait(lambda: _status(store, job.id) == JobStatus.running)

    # Closing the window mid-run must NOT pause a non-resumable pass; it runs to
    # completion (only the pre-start gate applies to it).
    allow["ok"] = False
    assert _wait(lambda: _status(store, job.id) == JobStatus.completed)
    assert engine.calls == 1
