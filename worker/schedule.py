"""Analysis schedule: the hours during which the queue may run analysis.

The queue holds analysis jobs and only runs them inside an allowed window, so a
library scan doesn't hammer the GPU while someone is watching TV. The schedule
is per-day-of-week: each weekday has its own allowed window. It is edited from
the Jellyfin plugin settings page and pushed here; the worker is the single
source of truth because the worker is what enforces it.

Time is worker-local wall-clock (``datetime.now()``). Renders are never gated —
only analysis passes — because a "Render clean copy" is an explicit, immediate
action.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .store import DATA_DIR

# Index 0..6 lines up with datetime.weekday() (Monday=0 .. Sunday=6).
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_PATH = DATA_DIR / "schedule.json"
_lock = threading.RLock()
_current: Optional["Schedule"] = None


class DayWindow(BaseModel):
    """One weekday's allowed window, in minutes since local midnight.

    - ``start < end``  → a same-day window, e.g. 02:00–07:00.
    - ``start > end``  → wraps past midnight into the next morning, e.g.
      23:00–07:00 means 23:00 today through 07:00 tomorrow.
    - ``start == end`` (and enabled) → allowed all day.
    - ``enabled`` false → analysis is never allowed to start on this weekday
      (except a wrap spilling in from the previous day).
    """

    enabled: bool = False
    start: int = 0  # [0, 1440)
    end: int = 0  # [0, 1440)


class Schedule(BaseModel):
    """A weekly analysis schedule.

    When ``enabled`` is false the schedule imposes no restriction — analysis
    runs whenever there is work, exactly as before this feature existed.
    """

    enabled: bool = False
    days: list[DayWindow] = Field(default_factory=lambda: [DayWindow() for _ in range(7)])


class ScheduleView(BaseModel):
    """The schedule plus a snapshot of whether analysis is allowed right now."""

    schedule: Schedule
    allowedNow: bool
    now: str  # worker-local, e.g. "Mon 21:15"


def _normalize(schedule: Schedule) -> Schedule:
    """Coerce to exactly seven days with in-range minute values."""
    days = list(schedule.days)[:7]
    while len(days) < 7:
        days.append(DayWindow())
    fixed = [
        DayWindow(
            enabled=bool(d.enabled),
            start=max(0, min(1439, int(d.start))),
            end=max(0, min(1439, int(d.end))),
        )
        for d in days
    ]
    return Schedule(enabled=bool(schedule.enabled), days=fixed)


def _load_from_disk() -> Schedule:
    if _PATH.exists():
        try:
            return _normalize(Schedule.model_validate_json(_PATH.read_text("utf-8")))
        except Exception:  # noqa: BLE001 — a corrupt file must not break the queue
            pass
    return Schedule()


def get_schedule() -> Schedule:
    """The current schedule, cached in memory (loaded from disk once)."""
    global _current
    with _lock:
        if _current is None:
            _current = _load_from_disk()
        return _current


def set_schedule(schedule: Schedule) -> Schedule:
    """Persist and cache a new schedule; returns the normalized value stored."""
    global _current
    norm = _normalize(schedule)
    with _lock:
        DATA_DIR.mkdir(exist_ok=True)
        _PATH.write_text(norm.model_dump_json(indent=2), "utf-8")
        _current = norm
    return norm


def is_allowed(now: datetime, schedule: Optional[Schedule] = None) -> bool:
    """Whether analysis may run at ``now`` under the schedule.

    An unrestricted (disabled) schedule always allows. Otherwise the moment must
    fall in the current weekday's window, or in the previous day's window where
    that window wraps past midnight into this morning.
    """
    schedule = schedule if schedule is not None else get_schedule()
    if not schedule.enabled:
        return True

    days = list(schedule.days)[:7]
    while len(days) < 7:
        days.append(DayWindow())

    mins = now.hour * 60 + now.minute
    weekday = now.weekday()

    today = days[weekday]
    if today.enabled:
        if today.start == today.end:
            return True  # all day
        if today.start < today.end:
            if today.start <= mins < today.end:
                return True
        else:  # wraps past midnight — the evening portion belongs to today
            if mins >= today.start:
                return True

    # The morning portion of a window that started (and wrapped) yesterday.
    prev = days[(weekday - 1) % 7]
    if prev.enabled and prev.start > prev.end and mins < prev.end:
        return True

    return False


def view(now: Optional[datetime] = None) -> ScheduleView:
    """The current schedule plus a right-now allowed snapshot, for the UI."""
    now = now or datetime.now()
    schedule = get_schedule()
    return ScheduleView(
        schedule=schedule,
        allowedNow=is_allowed(now, schedule),
        now=f"{DAY_NAMES[now.weekday()]} {now.hour:02d}:{now.minute:02d}",
    )
