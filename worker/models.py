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


class SegmentPatch(BaseModel):
    approved: Optional[bool] = None
    recommendedAction: Optional[str] = None


class RenderRequest(BaseModel):
    outputPath: Optional[str] = None
