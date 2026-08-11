"""Analysis schedule: the hours during which the queue may run analysis.

The queue holds analysis jobs and only runs them inside an allowed window, so a
library scan doesn't hammer the GPU while someone is watching TV. The schedule
is per-day-of-week: each weekday has its own allowed window. It is edited from
the Jellyfin plugin settings page and pushed here; the worker is the single
source of truth because the worker is what enforces it.

Time is evaluated in the schedule's own timezone (``Schedule.tz``, an IANA name
like ``America/Denver`` captured from the admin's browser when they save). This
keeps the window anchored to the person who set it up rather than to whatever
clock the worker box happens to run — a Docker container defaulting to UTC would
otherwise fire hours off from what the admin meant. When ``tz`` is unset we fall
back to worker-local wall-clock (the original behaviour). Renders are never
gated — only analysis passes — because a "Render clean copy" is an explicit,
immediate action.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .store import DATA_DIR

try:  # zoneinfo is stdlib on 3.9+, but IANA data needs `tzdata` on Windows.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — 3.11+ always has it, kept defensive.
    ZoneInfo = None  # type: ignore[assignment, misc]

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
    # IANA timezone the windows are expressed in (e.g. "America/Denver"), set
    # from the admin's browser. Empty means worker-local wall-clock.
    tz: str = ""


class ScheduleView(BaseModel):
    """The schedule plus a snapshot of whether analysis is allowed right now."""

    schedule: Schedule
    allowedNow: bool
    now: str  # wall-clock in the schedule's timezone, e.g. "Mon 21:15"


def _valid_tz(name: str) -> str:
    """Return ``name`` if it is a usable IANA zone, else "" (worker-local)."""
    name = (name or "").strip()
    if not name or ZoneInfo is None:
        return ""
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unknown/missing zone falls back to local
        return ""
    return name


def _wall_clock(now: datetime, tz: str) -> datetime:
    """Express ``now`` as wall-clock in the schedule's zone.

    A naive ``now`` (tests, legacy callers) is taken as already-local and
    returned unchanged. An aware ``now`` is converted to ``tz`` when set, or to
    the worker's local zone otherwise — preserving the pre-timezone behaviour.
    """
    if now.tzinfo is None:
        return now
    if tz and ZoneInfo is not None:
        try:
            return now.astimezone(ZoneInfo(tz))
        except Exception:  # noqa: BLE001 — bad zone falls back to worker-local
            pass
    return now.astimezone()


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
    return Schedule(enabled=bool(schedule.enabled), days=fixed, tz=_valid_tz(schedule.tz))


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

    local = _wall_clock(now, schedule.tz)
    mins = local.hour * 60 + local.minute
    weekday = local.weekday()

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
    now = now or datetime.now(timezone.utc)
    schedule = get_schedule()
    local = _wall_clock(now, schedule.tz)
    return ScheduleView(
        schedule=schedule,
        allowedNow=is_allowed(now, schedule),
        now=f"{DAY_NAMES[local.weekday()]} {local.hour:02d}:{local.minute:02d}",
    )
