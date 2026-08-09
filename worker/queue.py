"""Single-GPU job queue: one background worker thread processes jobs in order."""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from pathlib import Path
from typing import Optional

from .engines import ENGINES
from .models import Job, JobCreate, JobStatus, Timeline
from .render import approved_for_render, render as render_clean
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

    def submit_media_render(
        self, media_path: str, output_path: Optional[str] = None
    ) -> Job:
        """Render a clean copy from a film's APPROVED sidecar findings, by path.

        Unlike ``submit_render`` — which renders one analysis job's own stored
        timeline through that engine's renderer — this reads the film's
        ``.cleanmedia.json`` sidecar, the source of truth for review decisions,
        and folds every approved skip/mute/blur into one clean copy via the
        combined renderer. It is what the Jellyfin film view calls: it knows a
        film by path, not by job id, and only approved findings are acted on.
        """
        from .review import load_timeline

        media = Path(media_path)
        if not media.is_file():
            raise FileNotFoundError(f"media not found: {media}")
        timeline = load_timeline(media)
        if timeline is None:
            raise ValueError(f"no analysis found for {media.name}; analyze it first")
        if not approved_for_render(timeline):
            raise ValueError(
                f"{media.name}: none of {len(timeline.segments)} finding(s) are "
                "approved — review and approve findings before rendering"
            )

        job = Job(
            id=uuid.uuid4().hex[:12],
            mediaPath=str(media),
            engine="render",  # a render-only job, not an analysis engine
            mediaFingerprint=timeline.mediaFingerprint,
            status=JobStatus.rendering,
            stage="queued for rendering",
        )
        if output_path:
            job.options["renderOutputPath"] = output_path
        self.store.save_job(job)
        self._queue.put(("render_media", job.id))
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
                elif kind == "render_media":
                    self._render_media(job)
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

    def _render_media(self, job: Job) -> None:
        """Render a clean copy straight from the sidecar's approved findings.

        The counterpart to ``submit_media_render``: no engine plan, no stored
        job timeline — just the reviewed sidecar and the combined renderer, so
        exactly what an administrator approved is what gets acted on.
        """
        from .review import load_timeline
        from .shots import media_duration

        media = Path(job.mediaPath)
        timeline = load_timeline(media)
        approved = approved_for_render(timeline) if timeline else []
        if not approved:
            # The sidecar changed (findings un-approved or deleted) between
            # submit and now — better to fail than render an empty diff.
            raise RuntimeError("no approved findings to render")

        default_out = media.parent / "cleaned" / f"{media.stem} (Clean){media.suffix}"
        output = Path(job.options.get("renderOutputPath") or default_out)

        # Skips shorten the film, so the renderer needs its true length to work
        # out the spans to keep. Mute/blur-only renders don't, so skip the probe.
        needs_duration = any(s.recommendedAction == "skip" for s in approved)
        duration = media_duration(media) if needs_duration else None

        assert timeline is not None  # approved is non-empty, so it loaded
        rendered = render_clean(
            media,
            Timeline(mediaFingerprint=timeline.mediaFingerprint, segments=approved),
            output,
            self._progress_cb(job),
            duration_s=duration,
        )

        job = self.store.get_job(job.id) or job
        job.renderedPath = str(rendered)
        job.status = JobStatus.rendered
        job.progress = 1.0
        job.stage = "rendered clean copy"
        self.store.save_job(job)
