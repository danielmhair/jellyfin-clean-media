"""Single-GPU job queue: one background worker thread processes jobs in order."""

from __future__ import annotations

import math
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import schedule as schedule_mod
from .engines import ENGINES
from .logging_config import get_logger
from .models import Job, JobCreate, JobStatus, Timeline
from .render import approved_for_render, render as render_clean
from .store import Store, media_fingerprint

log = get_logger("queue")


def _name(job: Job) -> str:
    """Just the file name, for readable log lines (paths are long)."""
    return Path(job.mediaPath).name


def _fmt_duration(seconds: float) -> str:
    """A compact human duration, e.g. '4.2s', '3m12s', '1h04m'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole = int(seconds)
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{(whole % 3600) // 60:02d}m"


def clean_output_path(media: Path) -> Path:
    """Where a film's clean copy should be written.

    Jellyfin groups multiple files in one *movie folder* as selectable versions
    of the same movie, as long as each file name begins — character for
    character — with the folder name, followed by ``" - <label>"``. So when the
    film sits in its own folder (``Movie (2014)/Movie (2014).mkv``) we write the
    clean copy alongside it as ``Movie (2014) - Clean.mkv``: it shows up as a
    "Clean" version to pick in the player, and the original is never touched.

    When the film is *not* in its own folder (a flat library, or an episode
    under ``Season 01/``), version grouping can't work — naming a file after the
    shared parent folder would either fail to group or collide — so we fall back
    to a ``cleaned/`` subfolder, which is safe but won't appear as a version.
    """
    # A dedicated per-movie folder is the standard Jellyfin layout and the only
    # one where the file name equals the folder name. That exact match is also
    # what version grouping requires, so it's the right condition to key on.
    if media.stem == media.parent.name:
        return media.parent / f"{media.parent.name} - Clean{media.suffix}"
    return media.parent / "cleaned" / f"{media.stem} (Clean){media.suffix}"


class JobCancelled(Exception):
    """Raised inside a running job to abort it when a cancel is requested."""


class JobPaused(Exception):
    """Raised inside a resumable job when the schedule window closes mid-run.

    The job is re-queued rather than failed; it resumes from its engine's
    checkpoint when the allowed window reopens.
    """


class JobQueue:
    def __init__(
        self,
        store: Store,
        *,
        allowed_fn: Optional[Callable[[datetime], bool]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        poll_s: float = 30.0,
    ):
        self.store = store
        # The queue order lives on the jobs themselves (``queuePosition``), not
        # in an in-memory FIFO, so it can be reordered and survives a restart.
        # The worker waits on this condition and, when woken (a submit, reorder,
        # requeue, resume, or the poll_s timeout), runs the lowest-position
        # runnable job. ``_paused`` holds the *start* of the next job — it never
        # preempts the one already running. ``_running_id`` is that job, so it is
        # excluded from the "waiting" count.
        self._cond = threading.Condition()
        self._paused = False
        self._running_id: Optional[str] = None
        # Ids asked to stop. A queued job is skipped when its turn comes; a
        # running job aborts at its next progress tick (see _progress_cb).
        self._cancelled: set[str] = set()
        self._cancel_lock = threading.Lock()
        # Schedule gating (injectable for tests). ``allowed_fn`` answers "may
        # analysis run at this moment?"; the queue holds analysis jobs outside
        # the window and pauses a resumable pass when the window closes.
        self._allowed_fn = allowed_fn or schedule_mod.is_allowed
        # Aware UTC so the schedule can convert into its own timezone; a naive
        # injected now_fn (tests) is still honoured as local wall-clock.
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep_fn = sleep_fn or time.sleep
        self._poll_s = poll_s
        # Re-enqueue anything a previous run left unfinished BEFORE the worker
        # starts, so it resumes in the original order rather than losing the
        # in-memory queue on restart.
        self._recover()
        self._worker = threading.Thread(target=self._loop, daemon=True, name="job-worker")
        self._worker.start()

    # -- crash/restart recovery ----------------------------------------------

    @staticmethod
    def _kind_for(job: Job) -> Optional[str]:
        """The queue 'kind' to reprocess a persisted job with, or None if done.

        Mirrors how the submit_* methods classify work: a queued/running job is
        an analysis pass; a rendering job is a sidecar render (engine=="render")
        or an engine-plan render (any other engine, via ``submit_render``).
        """
        if job.status in (JobStatus.queued, JobStatus.running):
            return "analyze"
        if job.status == JobStatus.rendering:
            return "render_media" if job.engine == "render" else "render"
        return None

    def _recover(self) -> None:
        """Ready jobs left unfinished by a previous run, in submission order.

        The processing order lives on the jobs (``queuePosition``); the worker
        selects the lowest-position runnable job straight from the store, so a
        restart doesn't lose the queue. What recovery must still do is reset the
        status of anything caught mid-flight and give any job that predates
        ``queuePosition`` a position derived from ``createdAt`` — so the order
        the administrator submitted (and reordered) is preserved across the
        restart. A pass interrupted mid-run reads as ``running``/``rendering``;
        it is reset so the UI shows it waiting and the worker re-runs it.
        Resumable analysis engines (the VLM) then resume from their on-disk
        checkpoint; others re-run from the start. Terminal jobs are untouched.
        """
        pending = [
            j for j in self.store.list_jobs()
            if j.status in (JobStatus.queued, JobStatus.running, JobStatus.rendering)
        ]
        pending.sort(key=lambda j: j.createdAt)  # oldest first == original order

        next_pos = self._alloc_position()
        recovered = 0
        for job in pending:
            if self._kind_for(job) is None:
                continue
            changed = False
            if job.status == JobStatus.running:
                # Interrupted analysis: back to queued so _analyze re-runs it.
                # Progress is kept so a resumable engine shows where it resumes.
                job.status = JobStatus.queued
                job.stage = "resuming after restart"
                changed = True
            elif job.status == JobStatus.rendering:
                job.stage = "resuming render after restart"
                changed = True
            if job.queuePosition is None:
                # Predates queuePosition: assign one in createdAt order so it
                # sorts after any already-positioned job but keeps its relative
                # place among the other unpositioned ones.
                job.queuePosition = next_pos
                next_pos += 1
                changed = True
            if changed:
                self.store.save_job(job)
            recovered += 1

        if recovered:
            log.info(
                "recovery: readied %d unfinished job(s) from the store, "
                "resuming in submission order", recovered,
            )

    # -- persisted-order selection -------------------------------------------

    def _alloc_position(self) -> int:
        """The next end-of-queue position: above every position now in the store."""
        positions = [
            j.queuePosition for j in self.store.list_jobs()
            if j.queuePosition is not None
        ]
        return (max(positions) + 1) if positions else 0

    @staticmethod
    def _order_key(job: Job) -> tuple:
        """Sort key: by position (unset last), then submission time."""
        pos = job.queuePosition if job.queuePosition is not None else math.inf
        return (pos, job.createdAt)

    def _select_runnable(self) -> Optional[Job]:
        """The lowest-position job that may start right now, or None.

        Runnable means: a render (``rendering`` — never schedule-gated) or an
        analysis job (``queued``) while the schedule window is open. Selection
        is schedule-aware so an analysis job blocked by the window is simply not
        picked yet — the worker never *commits* to a job while it waits, which is
        what lets a reorder made during the wait take effect. Nothing is picked
        while the queue is paused (but a job already running is not preempted).
        """
        if self._paused:
            return None
        allowed = self._analysis_allowed()
        best: Optional[Job] = None
        best_key: Optional[tuple] = None
        for job in self.store.list_jobs():
            if job.status == JobStatus.rendering:
                pass  # renders are never gated by the schedule
            elif job.status == JobStatus.queued:
                if not allowed:
                    continue
            else:
                continue
            if job.id in self._cancelled:
                continue  # cancelled mid-flight, status not yet reflected
            key = self._order_key(job)
            if best_key is None or key < best_key:
                best, best_key = job, key
        return best

    def _claim_next(self) -> Job:
        """Block until a runnable job exists, then mark and return it.

        Woken by ``_cond`` on any queue change, and every ``poll_s`` regardless,
        so a schedule window opening or a paused flag clearing is re-evaluated
        without an explicit signal.
        """
        while True:
            with self._cond:
                job = self._select_runnable()
                if job is not None:
                    self._running_id = job.id
                    return job
                self._announce_schedule_hold()
                self._cond.wait(timeout=self._poll_s)

    def _announce_schedule_hold(self) -> None:
        """Mark queued analysis jobs as waiting when the window is what holds them.

        Selection is schedule-aware, so a held analysis job is never claimed and
        the old ``_await_schedule`` no longer runs to set its stage. Restore that
        signal here: while the window is shut (and the queue isn't paused), any
        queued job's stage reads "waiting for scheduled hours". Written only on
        the transition, so it is not a per-poll write.
        """
        if self._paused or self._analysis_allowed():
            return
        for job in self.store.list_jobs():
            if (
                job.status == JobStatus.queued
                and job.id not in self._cancelled
                and job.stage != "waiting for scheduled hours"
            ):
                job.stage = "waiting for scheduled hours"
                self.store.save_job(job)
                log.info(
                    "job %s waiting for scheduled hours (%s on %s)",
                    job.id, job.engine, _name(job),
                )

    def _signal(self) -> None:
        """Wake the worker to re-evaluate what to run (after any queue change)."""
        with self._cond:
            self._cond.notify_all()

    def _analysis_allowed(self) -> bool:
        return bool(self._allowed_fn(self._now_fn()))

    def submit(self, req: JobCreate) -> Job:
        media = Path(req.mediaPath)
        if not media.is_file():
            raise FileNotFoundError(f"media not found: {media}")
        if req.engine not in ENGINES:
            raise ValueError(f"unknown engine '{req.engine}'; installed: {list(ENGINES)}")

        fingerprint = media_fingerprint(media)
        duplicate = self.store.find_completed_by_fingerprint(fingerprint, req.engine)
        if duplicate:
            log.info(
                "job %s: reusing completed %s result for %s (no re-analysis)",
                duplicate.id, req.engine, media.name,
            )
            return duplicate

        job = Job(
            id=uuid.uuid4().hex[:12],
            mediaPath=str(media),
            engine=req.engine,
            mediaFingerprint=fingerprint,
            options=req.options,
        )
        with self._cond:
            job.queuePosition = self._alloc_position()
            self.store.save_job(job)
            self._cond.notify_all()
        log.info(
            "job %s queued: %s on %s (queue depth %d)",
            job.id, req.engine, media.name, self.queue_size(),
        )
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
        with self._cond:
            job.queuePosition = self._alloc_position()
            self.store.save_job(job)
            self._cond.notify_all()
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
        with self._cond:
            job.queuePosition = self._alloc_position()
            self.store.save_job(job)
            self._cond.notify_all()
        return job

    def queue_size(self) -> int:
        """How many jobs are waiting to start (queued/rendering, not the running one)."""
        with self._cond:
            running = self._running_id
        n = 0
        for job in self.store.list_jobs():
            if (
                job.status in (JobStatus.queued, JobStatus.rendering)
                and job.id != running
                and job.id not in self._cancelled
            ):
                n += 1
        return n

    # -- reorder / requeue / pause -------------------------------------------

    def reorder(self, ids: list[str]) -> list[Job]:
        """Reassign queue positions from a front-first list of job ids.

        Only jobs currently queued/rendering are repositioned (0..n in the given
        order); running, terminal and unknown ids are ignored, so a running pass
        is never preempted. Returns the full job list so the caller re-renders
        from the stored truth rather than its own optimistic order.
        """
        with self._cond:
            pending = {
                j.id: j for j in self.store.list_jobs()
                if j.status in (JobStatus.queued, JobStatus.rendering)
            }
            pos = 0
            for job_id in ids:
                job = pending.get(job_id)
                if job is None:
                    continue
                job.queuePosition = pos
                pos += 1
                self.store.save_job(job)
            self._cond.notify_all()
        return self.store.list_jobs()

    def requeue(self, job_id: str) -> Job:
        """Retry a failed or cancelled job from the back of the queue.

        Its progress, error and stage are cleared and it gets a fresh
        end-of-queue position (then it can be dragged to the front). Analysis
        jobs return to ``queued``; a render job returns to ``rendering`` so it is
        re-run as a render, not re-analyzed. Raises ``KeyError`` for an unknown
        id and ``ValueError`` for a job that is not failed/cancelled.
        """
        with self._cond:
            job = self.store.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status not in (JobStatus.failed, JobStatus.cancelled):
                raise ValueError(
                    f"job {job_id} is {job.status.value}, not a failed or "
                    "cancelled job that can be requeued"
                )
            is_render = job.engine == "render"
            job.status = JobStatus.rendering if is_render else JobStatus.queued
            job.progress = 0.0
            job.error = None
            job.stage = "queued for rendering" if is_render else "queued"
            job.queuePosition = self._alloc_position()
            self.store.save_job(job)
            with self._cancel_lock:
                self._cancelled.discard(job_id)
            self._cond.notify_all()
        return job

    def set_paused(self, paused: bool) -> bool:
        """Hold (or release) the start of the next job. Never preempts a running one."""
        with self._cond:
            self._paused = bool(paused)
            self._cond.notify_all()
        return self._paused

    def is_paused(self) -> bool:
        return self._paused

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
        self._signal()  # re-evaluate: a cancelled queued job must not be picked
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
            job = self._claim_next()  # blocks until a runnable job is available
            job_id = job.id
            kind = self._kind_for(job)
            if kind is None or job.status == JobStatus.cancelled:
                # Raced to a terminal/cancelled state between select and claim.
                with self._cond:
                    self._running_id = None
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
            except JobPaused:
                # The schedule window closed mid-run: re-queue to resume from the
                # engine's checkpoint when it reopens, rather than fail or lose
                # the work done so far. Its position is kept, so it holds its
                # place in line and resumes first when the window reopens.
                job = self.store.get_job(job_id) or job
                job.status = JobStatus.queued
                job.stage = "paused — outside scheduled hours"
                self.store.save_job(job)
                log.info(
                    "job %s paused at %.0f%% (outside scheduled hours) - will resume",
                    job_id, (job.progress or 0.0) * 100,
                )
            except JobCancelled:
                # Requested stop, not a failure: record it as cancelled.
                job = self.store.get_job(job_id) or job
                job.status = JobStatus.cancelled
                job.stage = "cancelled"
                self.store.save_job(job)
                log.info("job %s cancelled (%s on %s)", job_id, job.engine, _name(job))
            except Exception as exc:  # noqa: BLE001 — job errors must not kill the worker
                job = self.store.get_job(job_id) or job
                job.status = JobStatus.failed
                job.error = f"{exc}\n{traceback.format_exc(limit=3)}"
                self.store.save_job(job)
                log.error(
                    "job %s FAILED (%s on %s): %s",
                    job_id, job.engine, _name(job), exc, exc_info=True,
                )
            finally:
                with self._cond:
                    self._running_id = None
                    # Wake at once so the next job starts without waiting out a
                    # full poll_s — matters for the FIFO-fast test cadence.
                    self._cond.notify_all()
                with self._cancel_lock:
                    self._cancelled.discard(job_id)

    def _progress_cb(self, job: Job, resumable: bool = False):
        # Progress fires many times a second; log only when a new 10% decile is
        # crossed or the stage text changes, so the log tracks the pass without
        # drowning in ticks. Held in a dict so the closure can mutate it.
        marks = {"decile": -1, "stage": None}

        def cb(fraction, stage):
            # Check first: cancel() has already written status=cancelled, and
            # saving progress here would clobber it back to running.
            if self._is_cancelled(job.id):
                raise JobCancelled()
            # A resumable pass yields here if the schedule window has closed, so
            # it re-queues and resumes later instead of running out of hours.
            if resumable and not self._analysis_allowed():
                raise JobPaused()
            advanced_decile = False
            if fraction is not None:
                job.progress = round(float(fraction), 4)
                decile = int(job.progress * 10)
                if decile > marks["decile"]:
                    marks["decile"] = decile
                    advanced_decile = True
            stage_changed = bool(stage) and stage != marks["stage"]
            if stage:
                marks["stage"] = stage
                job.stage = stage
            # One DEBUG line per pass step: a new 10% decile (with the current
            # stage), or a stage change on its own if progress didn't advance.
            if advanced_decile:
                log.debug(
                    "job %s: %3d%% - %s", job.id,
                    int(job.progress * 100), stage or job.stage or "",
                )
            elif stage_changed:
                log.debug("job %s: %s", job.id, stage)
            self.store.save_job(job)

        return cb

    def _await_schedule(self, job: Job) -> None:
        """Block until analysis is allowed by the schedule.

        Applies to every analysis job before it starts, so nothing kicks off
        outside the window. Raises :class:`JobCancelled` if the job is cancelled
        while waiting. A no-op when the schedule is unrestricted.
        """
        announced = False
        while not self._analysis_allowed():
            if self._is_cancelled(job.id):
                raise JobCancelled()
            if not announced:
                job.status = JobStatus.queued
                job.stage = "waiting for scheduled hours"
                self.store.save_job(job)
                announced = True
                log.info(
                    "job %s waiting for scheduled hours (%s on %s)",
                    job.id, job.engine, _name(job),
                )
            self._sleep_fn(self._poll_s)

    def _analyze(self, job: Job) -> None:
        engine = ENGINES[job.engine]
        # Hold before starting so a pass never begins outside the window; a
        # resumable pass can also pause mid-run (see _progress_cb).
        self._await_schedule(job)

        job.status = JobStatus.running
        job.stage = "analyzing"
        self.store.save_job(job)
        started = time.monotonic()
        log.info("job %s started: %s on %s", job.id, job.engine, _name(job))

        timeline, plan_path = engine.analyze(
            Path(job.mediaPath),
            job.mediaFingerprint or "",
            job.options,
            self._progress_cb(job, resumable=getattr(engine, "resumable", False)),
        )
        self.store.save_timeline(job.id, timeline)

        job = self.store.get_job(job.id) or job
        job.enginePlanPath = str(plan_path) if plan_path else None
        job.status = JobStatus.completed
        job.progress = 1.0
        job.stage = f"found {len(timeline.segments)} segment(s)"
        self.store.save_job(job)
        log.info(
            "job %s completed: %s on %s - %d finding(s) in %s",
            job.id, job.engine, _name(job), len(timeline.segments),
            _fmt_duration(time.monotonic() - started),
        )

    def _render(self, job: Job) -> None:
        engine = ENGINES[job.engine]
        timeline = self.store.get_timeline(job.id)
        if not timeline or not job.enginePlanPath:
            raise RuntimeError("no analysis results to render from")

        media = Path(job.mediaPath)
        output = Path(job.options.get("renderOutputPath") or clean_output_path(media))
        started = time.monotonic()
        log.info("job %s rendering: %s (%s) -> %s", job.id, media.name, job.engine, output)

        rendered = engine.render(
            media, Path(job.enginePlanPath), timeline, output, self._progress_cb(job)
        )

        job = self.store.get_job(job.id) or job
        job.renderedPath = str(rendered)
        job.status = JobStatus.rendered
        job.progress = 1.0
        job.stage = "rendered clean copy"
        self.store.save_job(job)
        log.info(
            "job %s rendered clean copy in %s -> %s",
            job.id, _fmt_duration(time.monotonic() - started), rendered,
        )

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

        output = Path(job.options.get("renderOutputPath") or clean_output_path(media))
        started = time.monotonic()
        log.info(
            "job %s rendering: %s -> %s (%d approved finding(s))",
            job.id, media.name, output, len(approved),
        )

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
        log.info(
            "job %s rendered clean copy in %s -> %s",
            job.id, _fmt_duration(time.monotonic() - started), rendered,
        )
