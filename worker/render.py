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

import os
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
    keeps: list[tuple[float, float]],
    blur_expr: Optional[str],
    mute_expr: Optional[str],
    audio_in: str = "0",
) -> str:
    """Blur/mute, then cut and re-join with trim+concat.

    concat preserves real durations, unlike select+setpts=N/FRAME_RATE/TB,
    which silently desyncs telecined sources whose container frame rate does
    not match the frames the decoder actually emits.

    ``audio_in`` is the input index the audio is drawn from — 0 for the source,
    or 1 when a voice-removed track is supplied as a second input.
    """
    n = len(keeps)
    parts: list[str] = []

    vsrc, asrc = "0:v", f"{audio_in}:a"
    if blur_expr:
        parts.append(f"[0:v]{blur_expr}[vb]")
        vsrc = "vb"
    if mute_expr:
        parts.append(f"[{audio_in}:a]{mute_expr}[ab]")
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
    audio_path: Optional[Path] = None,
    audio_sr: Optional[int] = None,
    audio_ch: Optional[int] = None,
) -> tuple[list[str], int, int]:
    """Build the FFmpeg command. Returns (cmd, n_blur, n_mute).

    ``audio_path`` supplies a pre-rendered voice-removed audio track (raw
    s16le) to use in place of the source audio, for voice-only mutes — the
    separation happens outside FFmpeg (see :mod:`worker.engines.voice_render`).
    """
    approved = [s for s in timeline.segments if s.approved is not False]
    blur = [s for s in approved if s.recommendedAction == "blur"]
    mute = [s for s in approved if s.recommendedAction == "mute"]
    skip = [s for s in approved if s.recommendedAction == "skip"]
    # Voice-only mutes never reach FFmpeg as a filter — the vocals are already
    # gone from audio_path — but they still count as work to render, and they
    # force an audio re-encode (the new track replaces the original).
    voice = [s for s in approved if s.recommendedAction == "voice"]
    if not blur and not mute and not skip and not voice:
        raise RuntimeError("timeline has no approved segments to render")

    if skip and not duration_s:
        raise ValueError("duration_s is required to render skips")

    cmd = ["ffmpeg", "-y", "-i", str(media_path)]
    audio_in = "0"
    if audio_path is not None:
        # Raw PCM carries no header, so its rate/layout must be declared before
        # -i; the audio then comes from this second input instead of the source.
        cmd += ["-f", "s16le", "-ar", str(audio_sr), "-ac", str(audio_ch), "-i", str(audio_path)]
        audio_in = "1"

    blur_expr = (
        f"gblur=sigma={blur_sigma}:enable='{_enable_expr(blur)}'" if blur else None
    )
    mute_expr = f"volume=enable='{_enable_expr(mute)}':volume=0" if mute else None

    if skip:
        keeps = keep_intervals(skip, duration_s)
        cmd += ["-filter_complex", _cut_filter_complex(keeps, blur_expr, mute_expr, audio_in)]
        # A cut shortens the timeline; copied subtitles would drift out of sync.
        cmd += ["-map", "[outv]", "-map", "[outa]", "-sn"]
    else:
        cmd += ["-map", "0:v:0", "-map", f"{audio_in}:a:0", "-map", "0:s?"]
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

    if mute or skip or voice:
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
    approved = [s for s in timeline.segments if s.approved is not False]
    voice = [s for s in approved if s.recommendedAction == "voice"]

    # Voice-only mutes need Demucs source separation, which FFmpeg can't do:
    # build a vocals-removed audio track first, then feed it to FFmpeg in place
    # of the original audio. Cleaned up in the finally below.
    audio_path = audio_sr = audio_ch = None
    if voice:
        from .engines.voice_render import (
            DEFAULT_CH,
            DEFAULT_SR,
            render_voice_removed_pcm,
        )

        audio_sr, audio_ch = DEFAULT_SR, DEFAULT_CH
        # mkstemp returns an OPEN fd; close it or Windows keeps the file locked,
        # so ffmpeg can't write it and the unlink in the finally throws WinError 32.
        _fd, _name = tempfile.mkstemp(suffix=".pcm")
        os.close(_fd)
        audio_path = Path(_name)
        # Scale the separation pass into the first 90% so the bar keeps moving;
        # FFmpeg's own progress (skips only) takes it the rest of the way.
        render_voice_removed_pcm(
            media_path,
            voice,
            audio_path,
            lambda f, s: progress(min(0.9, f * 0.9), s),
            sr=audio_sr,
            ch=audio_ch,
        )

    try:
        return _run_render(
            media_path, timeline, output_path, progress, use_nvenc, duration_s,
            audio_path, audio_sr, audio_ch,
        )
    finally:
        if audio_path is not None:
            audio_path.unlink(missing_ok=True)


def _run_render(
    media_path: Path,
    timeline: Timeline,
    output_path: Path,
    progress: ProgressCb,
    use_nvenc: bool,
    duration_s: Optional[float],
    audio_path: Optional[Path],
    audio_sr: Optional[int],
    audio_ch: Optional[int],
) -> Path:
    # Rendering over an existing clean copy is routine — you find a missed word
    # while watching the clean version and re-render from it — but ffmpeg's -y
    # truncates its output the instant it starts, so a failure part-way through
    # would destroy a good copy, and reading and writing one path cannot work at
    # all. Write beside the target and swap in only once the new file is
    # complete: the old copy stays playable throughout, at the cost of holding
    # both for the length of the render.
    final_path = output_path
    try:
        in_place = output_path.exists()
    except OSError:
        in_place = False
    if in_place:
        output_path = final_path.with_name(
            f"{final_path.stem}.cm-partial{final_path.suffix}"
        )

    cmd, n_blur, n_mute = build_command(
        media_path, timeline, output_path, use_nvenc=use_nvenc, duration_s=duration_s,
        audio_path=audio_path, audio_sr=audio_sr, audio_ch=audio_ch,
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
    try:
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
        if in_place:
            # Same directory, so this is a rename: the old copy is replaced only
            # now that a complete new one exists.
            os.replace(output_path, final_path)
    except BaseException:
        if in_place:
            output_path.unlink(missing_ok=True)  # never leave a half file behind
        raise

    progress(1.0, f"wrote {final_path.name}")
    return final_path
