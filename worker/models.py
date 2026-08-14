"""Pydantic models: jobs and the standard timeline format shared by all engines."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    rendering = "rendering"
    rendered = "rendered"


class Segment(BaseModel):
    """One detected segment in the standard timeline format."""

    id: int
    startMs: int
    endMs: int
    category: str
    confidence: float
    engine: str
    recommendedAction: str = "blur"
    approved: Optional[bool] = None
    reasoning: Optional[str] = None
    # Engine-specific reference (e.g. PureFrame shot index) so edits can be
    # mapped back onto the engine's own plan file at render time.
    engineRef: Optional[str] = None


class Timeline(BaseModel):
    """Standard timeline format — every engine converts into this."""

    schemaVersion: int = SCHEMA_VERSION
    mediaFingerprint: str
    segments: list[Segment] = Field(default_factory=list)
    #: High-water mark for id allocation. Without it, deleting the newest
    #: finding would hand its id to the next one created, and a review UI
    #: holding the old id would silently act on the wrong segment.
    #: Absent in timelines written before this existed; 0 means "derive it".
    nextSegmentId: int = 0


class JobCreate(BaseModel):
    mediaPath: str
    engine: str = "pureframe"
    options: dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    id: str
    mediaPath: str
    engine: str
    status: JobStatus = JobStatus.queued
    progress: float = 0.0
    stage: str = ""
    error: Optional[str] = None
    mediaFingerprint: Optional[str] = None
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updatedAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    options: dict[str, Any] = Field(default_factory=dict)
    # Path to the engine's native output (e.g. the PureFrame censor plan).
    enginePlanPath: Optional[str] = None
    # Path of the rendered clean copy, once rendering has completed.
    renderedPath: Optional[str] = None
    #: Persisted queue order (lower = sooner). The worker always runs the
    #: lowest-position runnable job, so an administrator can reorder what runs
    #: next and the order survives a reload and a worker restart. Unset on rows
    #: written before this existed; recovery derives one from ``createdAt``.
    queuePosition: Optional[int] = None


class SegmentPatch(BaseModel):
    approved: Optional[bool] = None
    recommendedAction: Optional[str] = None
    #: Timing edits from the review UI, when a skip cuts off dialogue or
    #: leaves part of a scene visible. Only applied when explicitly sent.
    startMs: Optional[int] = None
    endMs: Optional[int] = None
    #: Editable description/note — e.g. correcting the stale shot reference on a
    #: duplicated finding. Only applied when explicitly sent.
    reasoning: Optional[str] = None
    #: Editable category, so a mis-categorised detection (a shirtless man read as
    #: nudity) can be corrected in review, not just its action. Only applied when
    #: explicitly sent.
    category: Optional[str] = None


class BulkApproval(BaseModel):
    """Apply one decision to many findings at once, from the review UI.

    `approved: null` clears the decision on all of them, the same as the
    single-segment patch — so a reviewer who filtered to one profane word
    can approve, reject or reset the whole group in one action.
    """

    ids: list[int] = Field(default_factory=list)
    approved: Optional[bool] = None


class SegmentMerge(BaseModel):
    """Combine several findings into one segment spanning all of them.

    Used to collapse a run of adjacent detections — a scene flagged shot by
    shot — into a single segment to skip. Defaults to an approved skip, since
    merging is itself the reviewer's decision.
    """

    ids: list[int] = Field(default_factory=list)
    recommendedAction: str = "skip"
    approved: Optional[bool] = True


class SegmentCreate(BaseModel):
    """A finding added by hand, for what the models missed."""

    startMs: int
    endMs: int
    category: str = "manual"
    recommendedAction: str = "skip"
    #: Adding a finding deliberately is itself the decision, so these
    #: default to approved rather than waiting for a second confirmation.
    approved: Optional[bool] = True
    reasoning: Optional[str] = None


class RenderRequest(BaseModel):
    outputPath: Optional[str] = None


class JobBrief(BaseModel):
    """Just enough of a job for the review UI to show progress."""

    id: str
    status: JobStatus
    progress: float = 0.0
    stage: str = ""
    error: Optional[str] = None
    #: Which engine this job runs, so the grid can label concurrent passes
    #: on one film ("visual 44%, profanity queued") rather than showing one.
    engine: str = ""


class MediaStatus(BaseModel):
    """Review state of one film, as the library grid needs it."""

    path: str
    #: Where the worker actually found the file, if anywhere.
    resolvedPath: Optional[str] = None
    analyzed: bool = False
    total: int = 0
    approved: int = 0
    rejected: int = 0
    pending: int = 0
    #: The headline job for a one-line status: a running/rendering pass wins
    #: over a queued one, which wins over the newest finished/failed job.
    job: Optional[JobBrief] = None
    #: Every in-flight job for this film (running/rendering/queued), most
    #: active first — so the grid can show each concurrent pass's own percent.
    jobs: list[JobBrief] = Field(default_factory=list)
    #: Analysis engines that have already completed for this film, so the UI
    #: can stop offering to re-run one that is already done.
    enginesDone: list[str] = Field(default_factory=list)


class StatusRequest(BaseModel):
    """Ask about a page of the library in one round trip."""

    paths: list[str] = Field(default_factory=list)


class ReorderRequest(BaseModel):
    """A new order for the queued jobs, front-first, by job id.

    Only ids that are currently queued/rendering are repositioned; running,
    terminal and unknown ids are ignored, so a running pass is never preempted.
    """

    ids: list[str] = Field(default_factory=list)


class PauseRequest(BaseModel):
    """Hold or release the start of the next job (never preempts a running one)."""

    paused: bool = True
