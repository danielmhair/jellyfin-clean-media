"""Combined renderer: apply an approved timeline to produce a clean copy.

Takes the engine-agnostic timeline and performs every approved action in a
single FFmpeg pass — blur, skip, and mute together. The original file is
never touched; output goes to a separate path.

Video is only re-encoded when a blur or skip requires it; a mute-only
render stream-copies the video so the picture stays bit-for-bit identical.

Skips shorten the film, which would desynchronise any copied subtitle
track, so subtitles are dropped when a skip is present.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .models import Segment, Timeline
from .engines.base import ProgressCb

BLUR_SIGMA = 30


def approved_for_render(timeline: Timeline) -> list[Segment]:
    """The findings a clean-copy render should act on: only the approved ones.

    Rejected (``approved is False``) and, crucially, undecided
    (``approved is None``) findings are excluded. Rendering is the "act" half
    of review → approve → act, so an unreviewed film renders nothing at all —
    never every detection at once. Callers pre-filter with this and hand the
    result to :func:`render`, whose own looser ``is not False`` guard then
    only ever sees already-approved segments.
    """
    return [s for s in timeline.segments if s.approved is True]


def _enable_expr(segments) -> str:
    return "+".join(
        f"between(t,{s.startMs / 1000:.3f},{s.endMs / 1000:.3f})" for s in segments
    )


def keep_intervals(skips, duration_s: float) -> list[tuple[float, float]]:
    """Invert skip ranges into the spans to keep."""
    ranges = sorted((s.startMs / 1000, s.endMs / 1000) for s in skips)
    keeps: list[tuple[float, float]] = []
    prev = 0.0
    for start, end in ranges:
        if start > prev:
            keeps.append((prev, start))
        prev = max(prev, end)
    if prev < duration_s:
        keeps.append((prev, duration_s))
    return keeps


def _cut_filter_complex(
    keeps: list[tuple[float, float]], blur_expr: Optional[str], mute_expr: Optional[str]
) -> str:
    """Blur/mute, then cut and re-join with trim+concat.

    concat preserves real durations, unlike select+setpts=N/FRAME_RATE/TB,
    which silently desyncs telecined sources whose container frame rate does
    not match the frames the decoder actually emits.
    """
    n = len(keeps)
    parts: list[str] = []

    vsrc, asrc = "0:v", "0:a"
    if blur_expr:
        parts.append(f"[0:v]{blur_expr}[vb]")
        vsrc = "vb"
    if mute_expr:
        parts.append(f"[0:a]{mute_expr}[ab]")
        asrc = "ab"

    parts.append(f"[{vsrc}]split={n}" + "".join(f"[vs{i}]" for i in range(n)))
    parts.append(f"[{asrc}]asplit={n}" + "".join(f"[as{i}]" for i in range(n)))
    for i, (start, end) in enumerate(keeps):
        parts.append(
            f"[vs{i}]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}]"
        )
        parts.append(
            f"[as{i}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
    joins = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{joins}concat=n={n}:v=1:a=1[outv][outa]")
    return ";".join(parts)


def build_command(
    media_path: Path,
    timeline: Timeline,
    output_path: Path,
    use_nvenc: bool = True,
    blur_sigma: int = BLUR_SIGMA,
    duration_s: Optional[float] = None,
) -> tuple[list[str], int, int]:
    """Build the FFmpeg command. Returns (cmd, n_blur, n_mute)."""
    approved = [s for s in timeline.segments if s.approved is not False]
    blur = [s for s in approved if s.recommendedAction == "blur"]
    mute = [s for s in approved if s.recommendedAction == "mute"]
    skip = [s for s in approved if s.recommendedAction == "skip"]
    if not blur and not mute and not skip:
        raise RuntimeError("timeline has no approved segments to render")

    if skip and not duration_s:
        raise ValueError("duration_s is required to render skips")

    cmd = ["ffmpeg", "-y", "-i", str(media_path)]
    blur_expr = (
        f"gblur=sigma={blur_sigma}:enable='{_enable_expr(blur)}'" if blur else None
    )
    mute_expr = f"volume=enable='{_enable_expr(mute)}':volume=0" if mute else None

    if skip:
        keeps = keep_intervals(skip, duration_s)
        cmd += ["-filter_complex", _cut_filter_complex(keeps, blur_expr, mute_expr)]
        # A cut shortens the timeline; copied subtitles would drift out of sync.
        cmd += ["-map", "[outv]", "-map", "[outa]", "-sn"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0", "-map", "0:s?"]
        if blur_expr:
            cmd += ["-vf", blur_expr]
        if mute_expr:
            cmd += ["-af", mute_expr]

    if blur or skip:
        if use_nvenc:
            # -cq 19 keeps the untouched majority of the film visually lossless
            cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    else:
        cmd += ["-c:v", "copy"]

    if mute or skip:
        cmd += ["-c:a", "ac3", "-b:a", "448k"]
    else:
        cmd += ["-c:a", "copy"]

    if not skip:
        cmd += ["-c:s", "copy"]
    cmd += [str(output_path)]
    return cmd, len(blur), len(mute)


def render(
    media_path: Path,
    timeline: Timeline,
    output_path: Path,
    progress: ProgressCb,
    use_nvenc: bool = True,
    duration_s: Optional[float] = None,
) -> Path:
    cmd, n_blur, n_mute = build_command(
        media_path, timeline, output_path, use_nvenc=use_nvenc, duration_s=duration_s
    )
    n_skip = len(
        [
            s
            for s in timeline.segments
            if s.approved is not False and s.recommendedAction == "skip"
        ]
    )
    # Global flags must precede the output path or ffmpeg ignores them.
    cmd = cmd[:1] + ["-nostdin", "-nostats", "-progress", "pipe:1"] + cmd[1:]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    progress(
        0.0, f"rendering: {n_blur} blur, {n_skip} skip, {n_mute} mute segment(s)"
    )

    # stderr goes to a file, never an undrained pipe: ffmpeg blocks forever
    # once a pipe nobody is reading fills up.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("out_time_ms=") and duration_s:
                try:
                    secs = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                progress(
                    min(secs / duration_s, 0.99), f"rendered {secs / 60:.1f} min"
                )
        proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError(f"ffmpeg render failed:\n{err.read()[-2000:]}")

    if not output_path.exists():
        raise RuntimeError(f"ffmpeg reported success but {output_path} is missing")
    progress(1.0, f"wrote {output_path.name}")
    return output_path
