"""Engine adapter interface.

Every AI detector plugs in as an adapter implementing this interface, so the
worker API stays stable regardless of which engine runs underneath.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

from ..models import Timeline

# progress callback: (fraction 0..1 or None, stage description)
ProgressCb = Callable[[Optional[float], str], None]


class EngineAdapter(ABC):
    name: str

    # Whether analyze() checkpoints its progress and resumes from where it left
    # off on a re-run. The job queue only pauses a running pass for a schedule
    # window if it can be resumed without losing work; non-resumable passes are
    # left to finish. Default False; a resumable engine opts in.
    resumable: bool = False

    @abstractmethod
    def version(self) -> str: ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Availability / model status. Must not raise."""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Detection categories and supported actions."""

    @abstractmethod
    def analyze(
        self,
        media_path: Path,
        fingerprint: str,
        options: dict[str, Any],
        progress: ProgressCb,
    ) -> tuple[Timeline, Optional[Path]]:
        """Analyze media, return (standard timeline, engine-native plan path)."""

    @abstractmethod
    def render(
        self,
        media_path: Path,
        plan_path: Path,
        timeline: Timeline,
        output_path: Path,
        progress: ProgressCb,
    ) -> Path:
        """Render a clean copy honoring reviewed segments. Never touches the original."""
