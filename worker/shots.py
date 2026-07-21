"""Shot detection with correct frame-rate handling.

Container frame rate lies on telecined DVD sources: an NTSC MPEG-2 rip
advertises 29.97fps while the decoder emits 23.976 progressive frames per
second. Converting frame numbers to timestamps with the advertised rate
puts every shot boundary at 0.8x its real time — silently, and the error
grows across the film. Always measure the rate the decoder actually
produces.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Shot:
    index: int
    start_frame: int
    end_frame: int
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def true_fps(media_path: Path) -> tuple[float, float, int]:
    """Return (fps, duration_s, frame_count) as the decoder actually emits them."""
    duration = float(
        subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(media_path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    # Decode-and-discard is the only reliable count: nb_frames is often absent
    # or wrong, and container fps cannot be trusted on telecined sources.
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(media_path),
            "-map", "0:v:0", "-f", "null", "-",
        ],
        capture_output=True, text=True, errors="replace",
    )
    frames = 0
    for token in proc.stderr.split():
        if token.startswith("frame="):
            frames = int(token.split("=", 1)[1] or 0)
    for line in reversed(proc.stderr.splitlines()):
        if "frame=" in line:
            part = line.split("frame=", 1)[1].strip().split()[0]
            if part.isdigit():
                frames = int(part)
            break
    if not frames or duration <= 0:
        raise RuntimeError(f"could not measure frame count for {media_path}")
    return frames / duration, duration, frames


def detect_shots(media_path: Path, threshold: float = 27.0) -> list[Shot]:
    """Detect shot boundaries, timed against the real decoded frame rate."""
    from scenedetect import ContentDetector, SceneManager, open_video

    fps, duration, frames = true_fps(media_path)

    video = open_video(str(media_path))
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold))
    manager.detect_scenes(video, show_progress=False)
    scenes = manager.get_scene_list()

    # Use scenedetect's own timecodes rather than converting its frame
    # numbers. On a telecined source it counts frames at the container rate
    # (29.97) while ffmpeg decodes at 23.976, so dividing its frame index by
    # the decode rate pushes later shots past the end of the file — which
    # silently dropped the last fifth of one film from a run, because every
    # frame grab beyond EOF just returned nothing.
    shots: list[Shot] = []
    for i, (start, end) in enumerate(scenes):
        start_s, end_s = start.get_seconds(), end.get_seconds()
        if start_s >= duration:
            break
        shots.append(
            Shot(i, start.get_frames(), end.get_frames(), start_s, min(end_s, duration))
        )
    if not shots:
        return [Shot(0, 0, frames, 0.0, duration)]

    # Guard the invariant directly rather than trusting it. Shot detection
    # has silently produced timestamps for a longer film than the one on
    # disk, and every sample past the end simply yields no frame — analysis
    # skips that stretch and still reports success.
    covered = max(s.end_s for s in shots)
    if covered < duration * 0.95:
        raise RuntimeError(
            f"shot detection covered only {covered:.0f}s of a {duration:.0f}s "
            f"film ({covered / duration:.0%}) — refusing to analyze a partial "
            "timeline"
        )
    return shots


def sample_times(
    shot: Shot, max_gap_s: float = 2.5, min_samples: int = 1
) -> list[float]:
    """Timestamps to inspect within a shot.

    One frame per shot is enough for a static shot, but a long take can pan,
    reveal a new character, or change what is on screen entirely — so longer
    shots get proportionally more samples, capped by max_gap_s. Sampling is
    inset from the boundaries to avoid dissolves and motion blur on the cut.

    min_samples guards the other failure mode: a single frame of a short
    shot can catch an unlucky moment (an actor mid-turn, a dark beat) and
    miss content that is plainly visible a second later.
    """
    span = shot.duration_s
    if span <= 0:
        return [shot.start_s]
    n = max(1, int(span // max_gap_s) + (1 if span > max_gap_s else 0))
    n = max(n, min_samples)
    if n == 1:
        return [shot.start_s + span / 2]
    inset = min(0.25, span / 10)
    usable = span - 2 * inset
    return [shot.start_s + inset + usable * i / (n - 1) for i in range(n)]


def load_shots(path: Path) -> list[Shot]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Shot(**s) for s in data]


def save_shots(shots: list[Shot], path: Path) -> None:
    path.write_text(
        json.dumps([s.__dict__ for s in shots], indent=1), encoding="utf-8"
    )
