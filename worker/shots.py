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


def media_duration(media_path: Path) -> float:
    """Wall-clock duration in seconds, straight from the container.

    Cheap (a single ffprobe, no decode) and reliable even on telecined
    sources: only the *frame rate* lies there, not the duration. Use this
    when a render needs the film's length to compute skip keeps but does not
    need the full decode-and-count that true_fps does.
    """
    return float(
        subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(media_path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )


def true_fps(media_path: Path) -> tuple[float, float, int]:
    """Return (fps, duration_s, frame_count) as the decoder actually emits them."""
    duration = media_duration(media_path)
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


# Reaching the true duration by scaling scenedetect's timeline up by more than
# this is not a frame-rate mislabel (telecine pulldown tops out at 29.97/23.976
# ≈ 1.25×) but a decode that stopped well short of the film — a genuinely
# partial timeline we refuse rather than smear across the whole runtime.
_MAX_TIMELINE_STRETCH = 1.5


def detect_shots(media_path: Path, threshold: float = 27.0) -> list[Shot]:
    """Detect shot boundaries, timed against the film's true duration.

    PySceneDetect times shots in the container's frame space. On a telecined
    MPEG-2 DVD rip that space runs at the advertised rate (29.97) while the film
    really plays at the decoded rate, so its timecodes come up short of the true
    runtime — by enough (5–20%) to look like a truncated analysis and, before,
    to fail the film outright. But the shot *positions* are right; only the
    seconds-per-frame is wrong, and uniformly so. So rescale scenedetect's whole
    timeline onto the reliable container duration: the last shot maps to the end,
    every shot lands proportionally, and no boundary can fall past EOF. A
    constant frame-rate lie is corrected exactly by a constant factor. A decode
    that genuinely stopped short would need too large a factor and is refused
    (see ``_MAX_TIMELINE_STRETCH``) rather than stretched over the whole film —
    so we neither drop the tail of a telecined rip nor analyze a partial one.
    """
    from scenedetect import ContentDetector, SceneManager, open_video

    fps, duration, frames = true_fps(media_path)

    video = open_video(str(media_path))
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold))
    manager.detect_scenes(video, show_progress=False)
    scenes = manager.get_scene_list()
    if not scenes:
        return [Shot(0, 0, frames, 0.0, duration)]

    # How far scenedetect thinks the film runs, in its own (frame-rate-lying)
    # timeline. Rescale that onto the true duration so full coverage is exact.
    span = scenes[-1][1].get_seconds()
    if span <= 0:
        raise RuntimeError(f"shot detection produced an empty timeline for {media_path}")
    scale = duration / span
    if scale > _MAX_TIMELINE_STRETCH:
        raise RuntimeError(
            f"shot detection covered only {span:.0f}s of a {duration:.0f}s "
            f"film ({span / duration:.0%}) — decode stopped short, refusing a "
            "partial timeline"
        )

    shots: list[Shot] = []
    for i, (start, end) in enumerate(scenes):
        start_s = min(duration, start.get_seconds() * scale)
        end_s = min(duration, end.get_seconds() * scale)
        if start_s >= duration:
            break
        shots.append(Shot(i, start.get_frames(), end.get_frames(), start_s, end_s))
    if not shots:
        return [Shot(0, 0, frames, 0.0, duration)]
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
