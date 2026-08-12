"""SQLite persistence for jobs and timelines.

Also writes the PRD-mandated sidecar file (`<media>.cleanmedia.json`) next to
the analyzed media so results survive independently of the worker database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Job, JobStatus, Segment, Timeline

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def default_db_path() -> Path:
    """Where the jobs DB lives, overridable via ``CLEANMEDIA_DB``.

    The override exists so the test suite (which imports ``worker.main`` and
    thereby instantiates a live ``Store``/``JobQueue``) can point at a throwaway
    database instead of the production one — without it, importing the app would
    open the real ``cleanmedia.db`` and its recovery would disturb a running job.
    """
    override = os.environ.get("CLEANMEDIA_DB")
    return Path(override) if override else DATA_DIR / "cleanmedia.db"


def media_fingerprint(path: Path) -> str:
    """Fast fingerprint for large media: size + sha256 of head/middle/tail chunks."""
    size = path.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    chunk = 8 * 1024 * 1024
    with path.open("rb") as f:
        for offset in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
            f.seek(offset)
            h.update(f.read(chunk))
    return f"quickhash:{h.hexdigest()}"


class Store:
    def __init__(self, db_path: Optional[Path] = None):
        DATA_DIR.mkdir(exist_ok=True)
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timelines (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    data TEXT NOT NULL
                );
                """
            )

    # -- jobs ---------------------------------------------------------------

    def save_job(self, job: Job) -> None:
        job.updatedAt = datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, data) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (job.id, job.model_dump_json()),
            )

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row["data"]) if row else None

    def list_jobs(self) -> list[Job]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT data FROM jobs").fetchall()
        jobs = [Job.model_validate_json(r["data"]) for r in rows]
        return sorted(jobs, key=lambda j: j.createdAt, reverse=True)

    def delete_job(self, job_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.execute("DELETE FROM timelines WHERE job_id = ?", (job_id,))
        return cur.rowcount > 0

    def find_completed_by_fingerprint(self, fingerprint: str, engine: str) -> Optional[Job]:
        """Duplicate-analysis detection: reuse results for identical media."""
        for job in self.list_jobs():
            if (
                job.mediaFingerprint == fingerprint
                and job.engine == engine
                and job.status in (JobStatus.completed, JobStatus.rendered)
            ):
                return job
        return None

    # -- timelines ----------------------------------------------------------

    def save_timeline(self, job_id: str, timeline: Timeline) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO timelines (job_id, data) VALUES (?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET data = excluded.data",
                (job_id, timeline.model_dump_json()),
            )
        job = self.get_job(job_id)
        if job:
            self._write_sidecar(job, timeline)

    def get_timeline(self, job_id: str) -> Optional[Timeline]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM timelines WHERE job_id = ?", (job_id,)
            ).fetchone()
        return Timeline.model_validate_json(row["data"]) if row else None

    def patch_segment(
        self, job_id: str, segment_id: int, approved=..., action=...
    ) -> Optional[Segment]:
        timeline = self.get_timeline(job_id)
        if not timeline:
            return None
        for seg in timeline.segments:
            if seg.id == segment_id:
                if approved is not ...:
                    seg.approved = approved
                if action is not ...:
                    seg.recommendedAction = action
                self.save_timeline(job_id, timeline)
                return seg
        return None

    def _write_sidecar(self, job: Job, timeline: Timeline) -> None:
        # Imported here, not at module scope: review imports store for the
        # fingerprint, so a top-level import would be circular.
        from .review import merge_segments

        media = Path(job.mediaPath)
        sidecar = media.with_name(media.stem + ".cleanmedia.json")
        try:
            prior: list[Segment] = []
            floor = 0
            if sidecar.is_file():
                existing = Timeline.model_validate_json(
                    sidecar.read_text(encoding="utf-8")
                )
                prior, floor = existing.segments, existing.nextSegmentId
            # Merge, never overwrite. This job ran one engine; the sidecar
            # may hold another engine's findings and an administrator's own
            # additions and decisions, none of which this job knows about.
            segments = merge_segments(prior, timeline.segments, {job.engine}, floor)
            merged = Timeline(
                mediaFingerprint=timeline.mediaFingerprint,
                segments=segments,
                nextSegmentId=max((s.id for s in segments), default=0) + 1,
            )
            sidecar.write_text(
                json.dumps(merged.model_dump(), indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # sidecar is best-effort; the DB remains the source of truth
