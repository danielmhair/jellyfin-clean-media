from pathlib import Path
import numpy as np
import ffmpeg
from .shots import ShotVerdict
from pureframe.utils.ffmpeg import extract_metadata, probe


def sample_keyframes(shot: ShotVerdict, n: int) -> list[int]:
    length = shot.end_frame - shot.start_frame
    if length <= n:
        return list(range(shot.start_frame, shot.end_frame))

    # Evenly spaced
    # If n=3: start, mid, end-1
    indices = np.linspace(shot.start_frame, shot.end_frame - 1, n, dtype=int)
    return sorted(list(set(indices)))


# PATCHED (jellyfin-clean-media): the upstream implementation decoded the whole
# movie from frame 0 for EVERY shot (select filter, no seek) and piped stderr
# without ever draining it, which deadlocks once ffmpeg fills the 64KB pipe
# buffer. This version seeks to the first requested frame, decodes only the
# requested span, kills ffmpeg once frames are collected, and leaves stderr
# uncaptured (inherited) with -loglevel error so it cannot deadlock.
def extract_frames(
    path: Path, frame_indices: list[int], downscale_max_edge: int
) -> dict[int, np.ndarray]:
    if not frame_indices:
        return {}

    meta = extract_metadata(probe(path))

    # Calculate scale
    w, h = meta.width, meta.height
    if w > h and w > downscale_max_edge:
        h = int(h * (downscale_max_edge / w))
        w = downscale_max_edge
    elif h > w and h > downscale_max_edge:
        w = int(w * (downscale_max_edge / h))
        h = downscale_max_edge
    w = w - (w % 2)
    h = h - (h % 2)

    frame_indices = sorted(set(int(i) for i in frame_indices))
    first, last = frame_indices[0], frame_indices[-1]
    span = last - first + 1
    # meta.fps can be a Fraction; a Fraction seek serializes as e.g. "5005/3",
    # which ffmpeg rejects as a duration — force plain float seconds.
    seek_ts = float(first) / float(meta.fps) if meta.fps else 0.0

    process = (
        ffmpeg.input(str(path), ss=seek_ts)
        .filter("scale", w, h)
        .output("pipe:", format="rawvideo", pix_fmt="bgr24", vframes=span, vsync=0)
        .global_args("-loglevel", "error", "-nostats")
        .run_async(pipe_stdout=True)
    )

    frame_size = w * h * 3
    wanted = set(frame_indices)
    results = {}

    try:
        for offset in range(span):
            in_bytes = process.stdout.read(frame_size)
            if not in_bytes or len(in_bytes) != frame_size:
                break
            frame_no = first + offset
            if frame_no in wanted:
                results[frame_no] = np.frombuffer(in_bytes, np.uint8).reshape(
                    [h, w, 3]
                )
                if len(results) == len(wanted):
                    break
    finally:
        try:
            process.stdout.close()
        except OSError:
            pass
        process.kill()
        process.wait()

    return results
