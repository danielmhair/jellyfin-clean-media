"""Single-GPU job queue: one background worker thread processes jobs in order."""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from pathlib import Path
from typing import Optional

from .engines import ENGINES
from .models import Job, JobCreate, JobStatus
from .store import Store, media_fingerprint


class JobCancelled(Exception):
    """Raised inside a running job to abort it when a cancel is requested."""


class JobQueue:
    def __init__(self, store: Store):
        self.store = store
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()  # (kind, job_id)
        # Ids asked to stop. A queued job is skipped when its turn comes; a
        # running job aborts at its next progress tick (see _progress_cb).
        self._cancelled: set[str] = set()
        self._cancel_lock = threading.Lock()
        self._worker = threading.Thread(target=self._loop, daemon=True, name="job-worker")
        self._worker.start()

    def submit(self, req: JobCreate) -> Job:
        media = Path(req.mediaPath)
        if not media.is_file():
            raise FileNotFoundError(f"media not found: {media}")
        if req.engine not in ENGINES:
            raise ValueError(f"unknown engine '{req.engine}'; installed: {list(ENGINES)}")

        fingerprint = media_fingerprint(media)
        duplicate = self.store.find_completed_by_fingerprint(fingerprint, req.engine)
        if duplicate:
            return duplicate

        job = Job(
            id=uuid.uuid4().hex[:12],
            mediaPath=str(media),
            engine=req.engine,
            mediaFingerprint=fingerprint,
            options=req.options,
        )
        self.store.save_job(job)
        self._queue.put(("analyze", job.id))
        return job

    def submit_render(self, job_id: str, output_path: Optional[str]) -> Job:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job.status not in (JobStatus.completed, JobStatus.rendered):
            raise ValueError(f"job {job_id} is not completed (status={job.status.value})")
        if output_path:
            job.options["renderOutputPath"] = output_path
        job.status = JobStatus.rendering
        job.progress = 0.0
        job.stage = "queued for rendering"
        self.store.save_job(job)
        self._queue.put(("render", job.id))
        return job

    def queue_size(self) -> int:
        return self._queue.qsize()

    # -- cancellation ---------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns True if it was still active.

        A queued job is marked cancelled and skipped when the worker reaches it;
        a running job is flagged so its next progress tick raises and unwinds.
        The record is left as ``cancelled`` rather than deleted, so a running
        pass cannot resurrect it by saving progress after the row is gone.
        """
        job = self.store.get_job(job_id)
        if not job or job.status not in (JobStatus.queued, JobStatus.running):
            return False
        with self._cancel_lock:
            self._cancelled.add(job_id)
        job.status = JobStatus.cancelled
        job.stage = "cancelled"
        self.store.save_job(job)
        return True

    def cancel_all(self) -> int:
        """Cancel every queued or running job. Returns how many were cancelled."""
        count = 0
        for job in self.store.list_jobs():
            if job.status in (JobStatus.queued, JobStatus.running) and self.cancel(job.id):
                count += 1
        return count

    def _is_cancelled(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancelled

    # -- worker thread --------------------------------------------------------

    def _loop(self) -> None:
        while True:
            kind, job_id = self._queue.get()
            job = self.store.get_job(job_id)
            if not job or job.status == JobStatus.cancelled:
                with self._cancel_lock:
                    self._cancelled.discard(job_id)
                continue
            try:
                if kind == "analyze":
                    self._analyze(job)
                else:
                    self._render(job)
            except JobCancelled:
                # Requested stop, not a failure: record it as cancelled.
                job = self.store.get_job(job_id) or job
                job.status = JobStatus.cancelled
                job.stage = "cancelled"
                self.store.save_job(job)
            except Exception as exc:  # noqa: BLE001 — job errors must not kill the worker
                job = self.store.get_job(job_id) or job
                job.status = JobStatus.failed
                job.error = f"{exc}\n{traceback.format_exc(limit=3)}"
                self.store.save_job(job)
            finally:
                with self._cancel_lock:
                    self._cancelled.discard(job_id)

    def _progress_cb(self, job: Job):
        def cb(fraction, stage):
            # Check first: cancel() has already written status=cancelled, and
            # saving progress here would clobber it back to running.
            if self._is_cancelled(job.id):
                raise JobCancelled()
            if fraction is not None:
                job.progress = round(float(fraction), 4)
            if stage:
                job.stage = stage
            self.store.save_job(job)

        return cb

    def _analyze(self, job: Job) -> None:
        engine = ENGINES[job.engine]
        job.status = JobStatus.running
        job.stage = "analyzing"
        self.store.save_job(job)

        timeline, plan_path = engine.analyze(
            Path(job.mediaPath), job.mediaFingerprint or "", job.options, self._progress_cb(job)
        )
        self.store.save_timeline(job.id, timeline)

        job = self.store.get_job(job.id) or job
        job.enginePlanPath = str(plan_path) if plan_path else None
        job.status = JobStatus.completed
        job.progress = 1.0
        job.stage = f"found {len(timeline.segments)} segment(s)"
        self.store.save_job(job)

    def _render(self, job: Job) -> None:
        engine = ENGINES[job.engine]
        timeline = self.store.get_timeline(job.id)
        if not timeline or not job.enginePlanPath:
            raise RuntimeError("no analysis results to render from")

        media = Path(job.mediaPath)
        default_out = media.parent / "cleaned" / f"{media.stem} (Clean){media.suffix}"
        output = Path(job.options.get("renderOutputPath") or default_out)

        rendered = engine.render(
            media, Path(job.enginePlanPath), timeline, output, self._progress_cb(job)
        )

        job = self.store.get_job(job.id) or job
        job.renderedPath = str(rendered)
        job.status = JobStatus.rendered
        job.progress = 1.0
        job.stage = "rendered clean copy"
        self.store.save_job(job)
