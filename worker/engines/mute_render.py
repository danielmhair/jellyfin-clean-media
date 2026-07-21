"""Shared FFmpeg mute renderer for audio-based engines.

Mutes approved segments in the first audio stream. The video stream is
stream-copied, so the picture is bit-for-bit identical to the original.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..models import Timeline
from .base import ProgressCb


def render_muted(
    media_path: Path, timeline: Timeline, output_path: Path, progress: ProgressCb
) -> Path:
    to_mute = [s for s in timeline.segments if s.approved is not False]
    if not to_mute:
        raise RuntimeError("no approved segments to mute")

    clauses = "+".join(
        f"between(t,{s.startMs / 1000:.3f},{s.endMs / 1000:.3f})" for s in to_mute
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(media_path),
        "-map", "0:v:0", "-map", "0:a:0", "-map", "0:s?",
        "-c:v", "copy", "-c:s", "copy",
        "-af", f"volume=enable='{clauses}':volume=0",
        "-c:a", "ac3", "-b:a", "448k",
        str(output_path),
    ]
    progress(0.0, f"muting {len(to_mute)} segment(s)")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg mute failed:\n{proc.stderr[-2000:]}")
    progress(1.0, "render complete")
    return output_path
