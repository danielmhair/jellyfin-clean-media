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


class JobQueue:
    def __init__(self, store: Store):
        self.store = store
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()  # (kind, job_id)
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

    # -- worker thread --------------------------------------------------------

    def _loop(self) -> None:
        while True:
            kind, job_id = self._queue.get()
            job = self.store.get_job(job_id)
            if not job or job.status == JobStatus.cancelled:
                continue
            try:
                if kind == "analyze":
                    self._analyze(job)
                else:
                    self._render(job)
            except Exception as exc:  # noqa: BLE001 — job errors must not kill the worker
                job = self.store.get_job(job_id) or job
                job.status = JobStatus.failed
                job.error = f"{exc}\n{traceback.format_exc(limit=3)}"
                self.store.save_job(job)

    def _progress_cb(self, job: Job):
        def cb(fraction, stage):
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
