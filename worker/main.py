"""Clean Media Worker API (FastAPI).

Run: uv run uvicorn worker.main:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from . import __version__
from .logging_config import configure_logging, get_logger, tame_uvicorn_loggers

# Configure logging before anything else imports/logs, so import-time messages
# (engine load, model warm-up) land in the same format and file.
_LOG_FILE = configure_logging()
log = get_logger("api")

from .engines import ENGINES
from .models import (
    BulkApproval,
    Job,
    JobBrief,
    JobCreate,
    JobStatus,
    MediaStatus,
    PauseRequest,
    RenderRequest,
    ReorderRequest,
    Segment,
    SegmentCreate,
    SegmentMerge,
    SegmentPatch,
    StatusRequest,
    Timeline,
)
from . import schedule
from .queue import JobQueue
from .schedule import Schedule, ScheduleView
from .review import (
    CLIP_PAD_S,
    build_clip,
    build_filmstrip,
    build_peaks,
    build_preview_clip,
    build_scrub_audio,
    create_segment,
    delete_segment,
    grab_thumbnail,
    library_view,
    load_timeline,
    merge_into_one,
    render_landing,
    render_page,
    media_runtime_ms,
    resolve_media,
    set_approvals,
    sidecar_exists,
    stream_command,
    update_segment,
    warm_media_index,
)
from .store import DATA_DIR, Store, media_fingerprint

@asynccontextmanager
async def _lifespan(app: FastAPI):
    _log_startup_banner()
    yield


app = FastAPI(title="Clean Media Worker", version=__version__, lifespan=_lifespan)

store = Store()
jobs = JobQueue(store)


# GET endpoints the UI/plugin poll on a timer — logged at DEBUG so a normal INFO
# log isn't drowned by the review grid refreshing every few seconds.
_QUIET_GET_PREFIXES = (
    "/api/status", "/api/jobs", "/api/health", "/api/segments",
    "/api/thumbnail", "/api/filmstrip", "/api/peaks", "/api/clip",
)

# Negative-lookup cache for /api/segments. The plugin polls this endpoint for
# every item a client plays; for an unanalyzed film there is no timeline, and
# `timeline_for` reaches that verdict by fingerprinting the media — a 24 MB read
# off the (often NAS) share, per poll. A client playing an unanalyzed film hits
# it ~1/sec, so we cache the "no analysis" answer briefly, keyed by the exact
# path the caller repeats, and short-circuit before touching the NAS. The TTL is
# short so a film that finishes analyzing starts serving segments within seconds;
# /api/reindex also clears it for an immediate refresh.
_SEGMENTS_NEG_TTL_S = 30.0
_segments_neg_cache: dict[str, float] = {}  # request path -> monotonic time cached
_segments_neg_lock = threading.Lock()


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with its status and how long it took.

    Mutations and errors log at INFO/WARNING so they stand out; routine GET
    polling logs at DEBUG. A slow request (>1s) is always surfaced at INFO so a
    stall is visible without turning on DEBUG.
    """
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.monotonic() - started) * 1000
        log.exception(
            "%s %s -> unhandled error after %.0fms",
            request.method, request.url.path, elapsed_ms,
        )
        raise
    elapsed_ms = (time.monotonic() - started) * 1000

    path = request.url.path
    quiet = request.method == "GET" and any(
        path.startswith(p) for p in _QUIET_GET_PREFIXES
    )
    if response.status_code >= 500:
        level = logging.ERROR
    elif response.status_code == 404 and quiet:
        # Expected for the plugin's polling endpoints: an unanalyzed film has no
        # segments/thumbnail yet, and a client playing it polls ~1/sec. Keep
        # these at DEBUG so they don't bury real warnings — but still surface a
        # pathologically slow one (a cold NAS walk) at INFO.
        level = logging.INFO if elapsed_ms >= 3000 else logging.DEBUG
    elif response.status_code >= 400:
        level = logging.WARNING
    elif quiet and elapsed_ms < 1000:
        level = logging.DEBUG
    else:
        level = logging.INFO
    log.log(
        level, "%s %s -> %d (%.0fms)",
        request.method, path, response.status_code, elapsed_ms,
    )
    return response


def _log_startup_banner() -> None:
    tame_uvicorn_loggers()
    roots = os.environ.get("CLEANMEDIA_MEDIA_ROOTS", "(none set)")
    gpu = _gpu_info()
    gpu_desc = gpu.get("name", "none") if gpu.get("available") else "none (CPU only)"
    sched = schedule.view()
    log.info("Clean Media Worker %s starting", __version__)
    log.info("  platform : %s", platform.platform())
    log.info("  gpu      : %s", gpu_desc)
    log.info("  engines  : %s", ", ".join(ENGINES) or "(none)")
    log.info("  media    : %s", roots)
    log.info("  data dir : %s", DATA_DIR)
    log.info("  log file : %s", _LOG_FILE or "(console only)")
    log.info(
        "  schedule : %s",
        f"restricted (now {sched.now}, allowed={sched.allowedNow})"
        if sched.schedule.enabled else "unrestricted (runs whenever queued)",
    )


def _warm_media_index_logged() -> None:
    log.info("media index: warming (walking media roots)...")
    started = time.monotonic()
    try:
        warm_media_index()
        log.info("media index: ready in %.1fs", time.monotonic() - started)
    except Exception:
        log.exception("media index: warm-up failed")


# Build the media-path index in the background at startup (overlapping model
# load) so the first review-grid /api/status doesn't wait on a cold walk of a
# large NAS share and time out the plugin.
threading.Thread(
    target=_warm_media_index_logged, name="media-index-warm", daemon=True
).start()


def _gpu_info() -> dict:
    try:
        import torch

        if torch.cuda.is_available():
            return {"available": True, "name": torch.cuda.get_device_name(0)}
        return {"available": False}
    except Exception:
        return {"available": False}


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "platform": platform.platform(),
        "gpu": _gpu_info(),
        "engines": {name: eng.health() for name, eng in ENGINES.items()},
        "queueSize": jobs.queue_size(),
        "paused": jobs.is_paused(),
    }


@app.get("/api/schedule", response_model=ScheduleView)
def get_schedule() -> ScheduleView:
    """The analysis schedule, plus whether analysis is allowed right now."""
    return schedule.view()


@app.put("/api/schedule", response_model=ScheduleView)
def put_schedule(new: Schedule) -> ScheduleView:
    """Replace the analysis schedule (edited from the Jellyfin settings page)."""
    saved = schedule.set_schedule(new)
    if saved.enabled:
        tz = saved.tz or "worker-local"
        enabled_days = ", ".join(
            f"{schedule.DAY_NAMES[i]} {d.start // 60:02d}:{d.start % 60:02d}-"
            f"{d.end // 60:02d}:{d.end % 60:02d}"
            for i, d in enumerate(saved.days) if d.enabled
        )
        log.info("schedule updated: restricted [%s] in %s", enabled_days or "no days", tz)
    else:
        log.info("schedule updated: unrestricted (analysis runs whenever queued)")
    return schedule.view()


@app.get("/api/capabilities")
def capabilities() -> dict:
    return {
        "engines": [
            {
                "name": name,
                "version": eng.version(),
                **eng.capabilities(),
            }
            for name, eng in ENGINES.items()
        ]
    }


@app.post("/api/jobs", response_model=Job, status_code=201)
def create_job(req: JobCreate) -> Job:
    # Jellyfin submits its own mount path (e.g. /media/Marvel/Film.mkv); map it
    # to the local file the worker can actually open, exactly as /api/status and
    # /api/segments do. Without this, analysis 404s for every non-local path.
    resolved = resolve_media(req.mediaPath)
    if resolved is not None:
        req.mediaPath = str(resolved)
    try:
        return jobs.submit(req)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/jobs", response_model=list[Job])
def list_jobs() -> list[Job]:
    return store.list_jobs()


@app.post("/api/jobs/cancel-all")
def cancel_all_jobs() -> dict:
    """Stop the whole batch: cancel every queued or running job at once."""
    return {"cancelled": jobs.cancel_all()}


@app.post("/api/jobs/reorder", response_model=list[Job])
def reorder_jobs(req: ReorderRequest) -> list[Job]:
    """Set the run order of the queued jobs, front-first, by id.

    Only queued/rendering jobs are repositioned; the running job and terminal
    jobs are left where they are, so a running pass is never interrupted. The
    updated job list is returned so the UI re-renders from stored truth.
    """
    return jobs.reorder(req.ids)


@app.post("/api/jobs/pause")
def pause_queue(req: PauseRequest) -> dict:
    """Hold or release the start of the next job (never preempts a running one)."""
    return {"paused": jobs.set_paused(req.paused), "queueSize": jobs.queue_size()}


@app.get("/api/jobs/{job_id}", response_model=Job)
def get_job(job_id: str) -> Job:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, f"job {job_id} not found")
    return job


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    # Cancel first so a running pass actually stops — deleting the row alone is
    # undone when the job next saves progress. If it was not active, forget it.
    if jobs.cancel(job_id):
        return
    if not store.delete_job(job_id):
        raise HTTPException(404, f"job {job_id} not found")


@app.get("/api/jobs/{job_id}/segments", response_model=Timeline)
def get_segments(job_id: str) -> Timeline:
    timeline = store.get_timeline(job_id)
    if not timeline:
        raise HTTPException(404, f"no timeline for job {job_id}")
    return timeline


@app.patch("/api/jobs/{job_id}/segments/{segment_id}", response_model=Segment)
def patch_segment(job_id: str, segment_id: int, patch: SegmentPatch) -> Segment:
    kwargs = {}
    if "approved" in patch.model_fields_set:
        kwargs["approved"] = patch.approved
    if "recommendedAction" in patch.model_fields_set:
        kwargs["action"] = patch.recommendedAction
    seg = store.patch_segment(job_id, segment_id, **kwargs)
    if not seg:
        raise HTTPException(404, f"segment {segment_id} not found in job {job_id}")
    return seg


@app.get("/api/library")
def library(q: str = "", limit: int = 50, offset: int = 0) -> dict:
    """The review switcher's library view. No ``q``: the work-list (analyzed
    films, needs-review first, then reviewed). With ``q``: fuzzy search over every
    video in every collection, including unanalyzed ones (which open for manual
    review). Each item carries its review status and undecided count."""
    return library_view(q, max(1, min(limit, 200)), max(0, offset))


@app.get("/api/review", response_class=HTMLResponse)
def review_page(path: str = "") -> HTMLResponse:
    """Administrator review UI.

    With no ``path`` it serves a landing page to browse/search the whole library
    and open any film (the entry point the plugin links to). With a ``path`` it
    opens the Studio for that film — for ANY existing video, even one with no
    analysis yet, so it can be reviewed by hand: scrub, add cuts at the playhead,
    set actions. The `.cleanmedia.json` sidecar is created on the first added
    segment, so analysis is an optional head-start, not a prerequisite.
    """
    if not path.strip():
        return HTMLResponse(render_landing())
    media = resolve_media(path)
    if media is None:
        literal = Path(path)
        media = literal if literal.is_file() else None
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    timeline = load_timeline(media)
    if timeline is None:
        # No analysis — open an empty Studio for manual review. The real
        # fingerprint is written when the first segment is added (create_segment).
        timeline = Timeline(mediaFingerprint="", segments=[])
    return HTMLResponse(render_page(media, timeline))


@app.get("/api/thumbnail")
def thumbnail(path: str, ms: int) -> Response:
    # resolve_media, not Path: callers include the Jellyfin plugin, which
    # knows the film by its own mount path.
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    jpeg = grab_thumbnail(media, ms)
    if not jpeg:
        raise HTTPException(500, f"could not extract a frame at {ms}ms")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/clip")
def clip(
    path: str,
    startMs: int,
    endMs: int,
    pad: float = CLIP_PAD_S,
    mute: bool = False,
    voice: bool = False,
) -> FileResponse:
    """A short, browser-playable clip around a finding, for review.

    With mute=true the flagged span is silenced, so a reviewer can hear the
    scene as it will play once the finding is acted on — the way to confirm a
    profanity mute lands on the word. With voice=true only the vocals are
    removed across the span (Demucs), so the reviewer hears the voice-only mute
    — the word gone, the music playing through.
    """
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    built = build_clip(media, startMs, endMs, pad, mute, voice)
    if built is None:
        raise HTTPException(500, "could not build clip")
    return FileResponse(built, media_type="video/mp4")


def _parse_spans(text: str) -> list[tuple[int, int]]:
    """Parse "a-b,a-b" ms spans from a query param; skip anything malformed."""
    spans: list[tuple[int, int]] = []
    for part in text.split(",") if text else []:
        a, _, b = part.partition("-")
        try:
            spans.append((int(a), int(b)))
        except ValueError:
            continue
    return spans


@app.get("/api/preview_clip")
def preview_clip(
    path: str, startMs: int, endMs: int, cut: str = "", mute: str = "", blur: str = ""
) -> FileResponse:
    """A cleaned preview of a window [startMs, endMs]: the ``cut`` spans are
    removed (their footage never transcoded), the ``mute`` spans silenced, and the
    ``blur`` spans blurred, so the review page can play the window as the viewer
    will experience it — with every applicable decision applied, not just one
    finding. Spans are comma-separated absolute-ms ``a-b`` pairs.
    """
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    built = build_preview_clip(
        media, startMs, endMs, _parse_spans(cut), _parse_spans(mute), _parse_spans(blur)
    )
    if built is None:
        raise HTTPException(500, "could not build preview clip")
    return FileResponse(built, media_type="video/mp4")


@app.get("/api/stream")
async def stream(
    request: Request, path: str, startMs: int = 0, cut: str = "", mute: str = "", blur: str = ""
) -> StreamingResponse:
    """Whole-film continuous playback (Phase 2): a live, browser-playable stream
    of the film from ``startMs`` to the end, transcoded on the fly to fragmented
    MP4 — so the reviewer can hold play and watch the film run across scenes, not
    just per-scene clips.

    With no ``cut``/``mute`` it is the original audio/picture (Normal; the page
    mutes the element for Muted). With them (Cleaned) the approved skips are cut
    out and mutes silenced **live over the whole remaining film** — the
    verify-the-invariant preview at film scale. Spans are comma-separated
    absolute-ms ``a-b`` pairs, as for ``/api/preview_clip``.

    Seeking is the page's job: it reloads this endpoint from a new ``startMs``.
    Each stream owns one ffmpeg process, killed when the client disconnects (the
    page swaps ``src``) so a re-seek never leaves a transcode running.
    """
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    timeline = load_timeline(media)
    runtime = media_runtime_ms(media, timeline) if timeline else startMs + 3_600_000
    if startMs >= runtime:
        raise HTTPException(416, "start past end of film")
    cmd = stream_command(
        media, startMs, runtime, _parse_spans(cut), _parse_spans(mute), _parse_spans(blur)
    )
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    async def pump():
        # Async so a client disconnect (the page swaps src to re-seek) runs the
        # finally promptly and kills ffmpeg — an orphaned transcode would steal
        # the GPU the analysis passes need. The disconnect check at the top of
        # the loop breaks us out at most one read after the client goes away.
        try:
            while True:
                if await request.is_disconnected():
                    break
                chunk = await run_in_threadpool(proc.stdout.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.stdout.close()
            except Exception:
                pass
            await run_in_threadpool(proc.wait)

    # No-store: the stream is live and offset-specific; caching it would break the
    # page's startMs→film-time mapping on a re-seek.
    return StreamingResponse(
        pump(), media_type="video/mp4", headers={"Cache-Control": "no-store"}
    )


@app.get("/api/scrub_audio")
def scrub_audio(path: str, startMs: int, endMs: int) -> FileResponse:
    """A compact mono WAV of [startMs, endMs] for the page's live scrub-audio —
    decoded once with WebAudio, then grains are played at the drag position so a
    reviewer hears the film as they scrub to find an edit point."""
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    built = build_scrub_audio(media, startMs, endMs)
    if built is None:
        raise HTTPException(500, "could not extract scrub audio")
    return FileResponse(
        built, media_type="audio/wav", headers={"Cache-Control": "max-age=3600"}
    )


@app.get("/api/peaks")
def peaks(path: str, startMs: int, endMs: int, pad: float = CLIP_PAD_S) -> dict:
    """Waveform peaks for the ±pad window around a finding, for the timing editor."""
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    result = build_peaks(media, startMs, endMs, pad)
    if result is None:
        raise HTTPException(500, "could not read audio peaks")
    return result


@app.get("/api/filmstrip")
def filmstrip(path: str, startMs: int, endMs: int, pad: float = CLIP_PAD_S) -> Response:
    """Tiled filmstrip of the ±pad window, for the visual timing editor."""
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    jpeg = build_filmstrip(media, startMs, endMs, pad)
    if not jpeg:
        raise HTTPException(500, "could not build filmstrip")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.patch("/api/segments", response_model=Timeline)
def approve_segments(path: str, req: BulkApproval) -> Timeline:
    """Apply one decision to many findings at once, by media path.

    The review UI calls this when a reviewer bulk-approves or -rejects the
    findings currently on screen — e.g. every instance of one profane word.
    One request, one sidecar write; see `set_approvals` for why that matters.
    """
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    set_approvals(media, req.ids, req.approved)
    timeline = load_timeline(media)
    if timeline is None:
        raise HTTPException(404, f"no analysis found for {media}")
    return timeline


@app.post("/api/segments/merge", response_model=Timeline)
def merge_segments_endpoint(path: str, req: SegmentMerge) -> Timeline:
    """Combine several findings into one segment spanning them all, by media path.

    The review UI calls this to collapse a run of adjacent detections (a scene
    the visual pass flagged shot by shot) into a single approved skip. The
    originals are removed and replaced by one MANUAL_ENGINE finding.
    """
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    if merge_into_one(media, req.ids, req.recommendedAction, req.approved) is None:
        raise HTTPException(400, "need at least two existing findings to merge")
    timeline = load_timeline(media)
    assert timeline is not None
    return timeline


@app.patch("/api/segments/{segment_id}", response_model=Timeline)
def approve_segment(segment_id: int, path: str, patch: SegmentPatch) -> Timeline:
    """Approve, reject or retime a finding, by media path rather than job id.

    This is what the review UI calls. Approved findings are the only ones
    the Jellyfin plugin will skip or a render will act on.

    Only fields actually sent are applied: `approved: null` means "clear the
    decision", which is different from "not mentioned".
    """
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")

    changes = {}
    sent = patch.model_fields_set
    if "approved" in sent:
        changes["approved"] = patch.approved
    if "startMs" in sent and patch.startMs is not None:
        changes["start_ms"] = patch.startMs
    if "endMs" in sent and patch.endMs is not None:
        changes["end_ms"] = patch.endMs
    if "recommendedAction" in sent and patch.recommendedAction is not None:
        changes["action"] = patch.recommendedAction
    if "reasoning" in sent:
        changes["reasoning"] = patch.reasoning or ""
    if "category" in sent and patch.category is not None:
        changes["category"] = patch.category

    if update_segment(media, segment_id, **changes) is None:
        raise HTTPException(404, f"segment {segment_id} not found for {media}")
    timeline = load_timeline(media)
    assert timeline is not None
    return timeline


def timeline_for(media: Path) -> Timeline | None:
    """Every finding for a film, merged across engines, or None if unanalyzed.

    Prefers the sidecar, which is where review decisions are written, and
    falls back to merging job timelines for media analyzed before a sidecar
    existed.
    """
    sidecar = media.with_name(media.stem + ".cleanmedia.json")
    if sidecar.is_file():
        return Timeline.model_validate_json(sidecar.read_text(encoding="utf-8"))

    fingerprint = media_fingerprint(media)
    merged: list[Segment] = []
    for job in store.list_jobs():
        if job.mediaFingerprint != fingerprint:
            continue
        found = store.get_timeline(job.id)
        if found:
            merged.extend(found.segments)
    if not merged:
        return None
    merged.sort(key=lambda s: s.startMs)
    for i, segment in enumerate(merged, 1):
        segment.id = i
    return Timeline(mediaFingerprint=fingerprint, segments=merged)


@app.delete("/api/segments/{segment_id}", response_model=Timeline)
def remove_segment(segment_id: int, path: str) -> Timeline:
    """Delete a finding outright, so noise does not accumulate."""
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    if not delete_segment(media, segment_id):
        raise HTTPException(404, f"segment {segment_id} not found for {media}")
    timeline = timeline_for(media)
    assert timeline is not None
    return timeline


@app.post("/api/segments", response_model=Segment, status_code=201)
def add_segment(path: str, req: SegmentCreate) -> Segment:
    """Add a finding the engines missed."""
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    segment = create_segment(
        media,
        req.startMs,
        req.endMs,
        req.category,
        req.recommendedAction,
        req.approved,
        req.reasoning,
    )
    if segment is None:
        raise HTTPException(500, f"could not add a finding for {media}")
    return segment


@app.get("/api/segments", response_model=Timeline)
def segments_for_media(path: str, approvedOnly: bool = True) -> Timeline:
    """Look up a reviewed timeline by media path.

    This is what the Jellyfin plugin calls: it knows a library item's file
    path but not our job ids. Results from every engine that has analyzed
    the file are merged, so one call returns skips, mutes and blurs
    together.
    """
    # A recent miss for this exact path? Answer 404 without the NAS round trip
    # `timeline_for` would otherwise cost (see _segments_neg_cache). Keyed by the
    # raw path so a repeated poll skips even resolve_media.
    now = time.monotonic()
    with _segments_neg_lock:
        cached = _segments_neg_cache.get(path)
        if cached is not None and now - cached < _SEGMENTS_NEG_TTL_S:
            raise HTTPException(404, f"no analysis found for {path}")

    def _remember_miss() -> None:
        with _segments_neg_lock:
            _segments_neg_cache[path] = now

    # Jellyfin sends its own mount path, which will not exist on this box.
    media = resolve_media(path)
    if media is None:
        _remember_miss()
        raise HTTPException(404, f"media not found: {path}")
    timeline = timeline_for(media)
    if timeline is None:
        _remember_miss()
        raise HTTPException(404, f"no analysis found for {media}")

    # Hit: drop any stale miss so a freshly analyzed film serves immediately.
    with _segments_neg_lock:
        _segments_neg_cache.pop(path, None)

    if approvedOnly:
        timeline.segments = [s for s in timeline.segments if s.approved is True]
    return timeline


@app.post("/api/status", response_model=list[MediaStatus])
def status_for_media(req: StatusRequest) -> list[MediaStatus]:
    """Review state for a page of the library, in one round trip.

    The Jellyfin review page lists items straight from Jellyfin, then asks
    here for the part only the worker knows: how many findings each film
    has, how many are still awaiting a decision, and whether analysis is
    queued or running.
    """
    # Match jobs by file name rather than fingerprint: fingerprinting reads
    # 24MB per film, which is far too slow for a whole page of a library. Keep
    # every job per name (newest first), not just one: a film can have several
    # engines queued/running at once, and a running pass hidden behind a queued
    # one of the same file read as "stuck".
    jobs_by_name: dict[str, list[Job]] = {}
    for job in store.list_jobs():  # newest first
        jobs_by_name.setdefault(Path(job.mediaPath).name.lower(), []).append(job)

    # Show a running/rendering pass before a queued one; both before anything
    # finished. Within a rank the newest wins (the list is already newest-first
    # and the sort is stable).
    active_rank = {JobStatus.running: 0, JobStatus.rendering: 1, JobStatus.queued: 2}

    def brief(job: Job) -> JobBrief:
        return JobBrief(
            id=job.id, status=job.status, progress=job.progress,
            stage=job.stage, error=job.error, engine=job.engine,
        )

    out: list[MediaStatus] = []
    for path in req.paths or []:
        status = MediaStatus(path=path)
        media = resolve_media(path)
        if media is None:
            out.append(status)
            continue

        status.resolvedPath = str(media)
        film_jobs = jobs_by_name.get(media.name.lower(), [])

        active = sorted(
            (j for j in film_jobs if j.status in active_rank),
            key=lambda j: active_rank[j.status],
        )
        status.jobs = [brief(j) for j in active]
        # Headline: the top active job, else the newest job of any kind (so a
        # lone "failed" still surfaces).
        headline = active[0] if active else (film_jobs[0] if film_jobs else None)
        status.job = brief(headline) if headline else None

        # Analysis engines already finished for this film, so the UI can stop
        # offering to re-run one that is done. "render" is not an analysis
        # engine, so restrict to the registered detectors.
        status.enginesDone = sorted({
            j.engine for j in film_jobs
            if j.status in (JobStatus.completed, JobStatus.rendered) and j.engine in ENGINES
        })

        # Reading the sidecar is a NAS round trip; skip it unless the cached
        # index says one exists, or a finished job means it was just written.
        # An unanalyzed library page then does zero per-film NAS I/O and stays
        # fast (the sequential per-film stat is what timed out the grid).
        just_analyzed = any(
            j.status in (JobStatus.completed, JobStatus.rendered) for j in film_jobs
        )
        if sidecar_exists(media) or just_analyzed:
            timeline = timeline_for(media)
            if timeline is not None:
                status.analyzed = True
                status.total = len(timeline.segments)
                status.approved = sum(1 for s in timeline.segments if s.approved is True)
                status.rejected = sum(1 for s in timeline.segments if s.approved is False)
                status.pending = status.total - status.approved - status.rejected
        out.append(status)
    return out


@app.post("/api/reindex")
def reindex() -> dict:
    """Rebuild the media/sidecar index now, instead of waiting out its TTL.

    The review grid answers "analyzed?" from a cached walk of the media roots.
    A sidecar written after that walk — by the batch tool, or copied in by
    hand — is invisible until the cache expires, so the grid shows a reviewed
    film as "not analyzed". The batch pings this when it finishes, and the
    grid can offer it as a manual refresh.
    """
    warm_media_index()
    # A film analyzed since the last walk now has a sidecar; forget any cached
    # "no analysis" misses so /api/segments reflects it at once.
    with _segments_neg_lock:
        _segments_neg_cache.clear()
    return {"status": "ok"}


@app.post("/api/jobs/{job_id}/requeue", response_model=Job)
def requeue_job(job_id: str) -> Job:
    """Retry a failed or cancelled job from the back of the queue."""
    try:
        return jobs.requeue(job_id)
    except KeyError:
        raise HTTPException(404, f"job {job_id} not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/jobs/{job_id}/render", response_model=Job, status_code=202)
def render(job_id: str, req: RenderRequest | None = None) -> Job:
    try:
        return jobs.submit_render(job_id, req.outputPath if req else None)
    except KeyError:
        raise HTTPException(404, f"job {job_id} not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/render", response_model=Job, status_code=202)
def render_media(path: str, req: RenderRequest | None = None) -> Job:
    """Render a clean copy from a film's APPROVED findings, by media path.

    This is what the Jellyfin film view calls. Jellyfin knows a film by its
    own mount path, not our job ids, so — like /api/segments and /api/status —
    it addresses the film by path. Only approved findings are acted on: mutes
    and blurs, which Jellyfin cannot apply during playback, folded together
    with any approved skips into one clean copy. The original is never touched;
    the job it returns is polled via /api/jobs/{id} for progress.
    """
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    try:
        return jobs.submit_media_render(str(media), req.outputPath if req else None)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
