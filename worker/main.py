"""Clean Media Worker API (FastAPI).

Run: uv run uvicorn worker.main:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import platform
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse

from . import __version__
from .engines import ENGINES
from .models import (
    BulkApproval,
    Job,
    JobBrief,
    JobCreate,
    JobStatus,
    MediaStatus,
    RenderRequest,
    Segment,
    SegmentCreate,
    SegmentPatch,
    StatusRequest,
    Timeline,
)
from .queue import JobQueue
from .review import (
    CLIP_PAD_S,
    build_clip,
    build_peaks,
    create_segment,
    delete_segment,
    grab_thumbnail,
    load_timeline,
    render_page,
    resolve_media,
    set_approvals,
    sidecar_exists,
    update_segment,
    warm_media_index,
)
from .store import Store, media_fingerprint

app = FastAPI(title="Clean Media Worker", version=__version__)

store = Store()
jobs = JobQueue(store)

# Build the media-path index in the background at startup (overlapping model
# load) so the first review-grid /api/status doesn't wait on a cold walk of a
# large NAS share and time out the plugin.
threading.Thread(target=warm_media_index, name="media-index-warm", daemon=True).start()


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
    }


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


@app.get("/api/review", response_class=HTMLResponse)
def review_page(path: str) -> HTMLResponse:
    """Administrator review UI for one film."""
    media = resolve_media(path) or Path(path)
    timeline = load_timeline(media)
    if timeline is None:
        raise HTTPException(404, f"no analysis found for {media}")
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
    path: str, startMs: int, endMs: int, pad: float = CLIP_PAD_S, mute: bool = False
) -> FileResponse:
    """A short, browser-playable clip around a finding, for review.

    With mute=true the flagged span is silenced, so a reviewer can hear the
    scene as it will play once the finding is acted on — the way to confirm a
    profanity mute lands on the word.
    """
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    built = build_clip(media, startMs, endMs, pad, mute)
    if built is None:
        raise HTTPException(500, "could not build clip")
    return FileResponse(built, media_type="video/mp4")


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
    # Jellyfin sends its own mount path, which will not exist on this box.
    media = resolve_media(path)
    if media is None:
        raise HTTPException(404, f"media not found: {path}")
    timeline = timeline_for(media)
    if timeline is None:
        raise HTTPException(404, f"no analysis found for {media}")

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
