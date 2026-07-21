"""Clean Media Worker API (FastAPI).

Run: uv run uvicorn worker.main:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import platform
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse

from . import __version__
from .engines import ENGINES
from .models import Job, JobCreate, RenderRequest, Segment, SegmentPatch, Timeline
from .queue import JobQueue
from .review import (
    CLIP_PAD_S,
    build_clip,
    grab_thumbnail,
    load_timeline,
    render_page,
    resolve_media,
    set_approval,
)
from .store import Store, media_fingerprint

app = FastAPI(title="Clean Media Worker", version=__version__)

store = Store()
jobs = JobQueue(store)


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
    try:
        return jobs.submit(req)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/jobs", response_model=list[Job])
def list_jobs() -> list[Job]:
    return store.list_jobs()


@app.get("/api/jobs/{job_id}", response_model=Job)
def get_job(job_id: str) -> Job:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, f"job {job_id} not found")
    return job


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
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
    media = Path(path)
    timeline = load_timeline(media)
    if timeline is None:
        raise HTTPException(404, f"no analysis found for {media}")
    return HTMLResponse(render_page(media, timeline))


@app.get("/api/thumbnail")
def thumbnail(path: str, ms: int) -> Response:
    media = Path(path)
    if not media.is_file():
        raise HTTPException(404, f"media not found: {media}")
    jpeg = grab_thumbnail(media, ms)
    if not jpeg:
        raise HTTPException(500, f"could not extract a frame at {ms}ms")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/clip")
def clip(path: str, startMs: int, endMs: int, pad: float = CLIP_PAD_S) -> FileResponse:
    """A short, browser-playable clip around a finding, for review."""
    media = Path(path)
    if not media.is_file():
        raise HTTPException(404, f"media not found: {media}")
    built = build_clip(media, startMs, endMs, pad)
    if built is None:
        raise HTTPException(500, "could not build clip")
    return FileResponse(built, media_type="video/mp4")


@app.patch("/api/segments/{segment_id}", response_model=Timeline)
def approve_segment(segment_id: int, path: str, patch: SegmentPatch) -> Timeline:
    """Approve or reject a finding, by media path rather than job id.

    This is what the review UI calls. Approved findings are the only ones
    the Jellyfin plugin will skip or a render will act on.
    """
    media = Path(path)
    if not set_approval(media, segment_id, patch.approved):
        raise HTTPException(404, f"segment {segment_id} not found for {media}")
    timeline = load_timeline(media)
    assert timeline is not None
    return timeline


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
    sidecar = media.with_name(media.stem + ".cleanmedia.json")
    if sidecar.is_file():
        timeline = Timeline.model_validate_json(sidecar.read_text(encoding="utf-8"))
    else:
        fingerprint = media_fingerprint(media)
        merged: list[Segment] = []
        for job in store.list_jobs():
            if job.mediaFingerprint != fingerprint:
                continue
            found = store.get_timeline(job.id)
            if found:
                merged.extend(found.segments)
        if not merged:
            raise HTTPException(404, f"no analysis found for {media}")
        merged.sort(key=lambda s: s.startMs)
        for i, segment in enumerate(merged, 1):
            segment.id = i
        timeline = Timeline(mediaFingerprint=fingerprint, segments=merged)

    if approvedOnly:
        timeline.segments = [s for s in timeline.segments if s.approved is True]
    return timeline


@app.post("/api/jobs/{job_id}/render", response_model=Job, status_code=202)
def render(job_id: str, req: RenderRequest | None = None) -> Job:
    try:
        return jobs.submit_render(job_id, req.outputPath if req else None)
    except KeyError:
        raise HTTPException(404, f"job {job_id} not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
