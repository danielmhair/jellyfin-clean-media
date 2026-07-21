"""OCR for image-based DVD subtitles (VobSub).

Most DVD rips carry only `dvd_subtitle` streams — bitmaps, not text — so
the subtitle engine cannot read them even though a human already
transcribed every line. OCR recovers that text, which measurably beats ASR
on the same dialogue.

Two details make the difference between garbage and near-perfect output:

* Track choice. A disc typically carries several English subtitle streams
  and most of them are *forced* tracks holding only on-screen signage.
  One disc tested here has three: two with 12 packets and one with 1333.
  Picking by language alone lands on a forced track and silently yields
  nothing.

* Compositing. DVD subtitles are white glyphs with a black outline over a
  transparent background. Flattening onto white leaves hollow outlines that
  OCR reads as noise ("SUPEMHIMMUNS SyStEMs"). Flattening onto black and
  then inverting gives clean black-on-white ("super-immune systems...").
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from .subtitles import Cue

IMAGE_SUB_CODECS = {"dvd_subtitle", "dvdsub", "hdmv_pgs_subtitle", "pgssub"}
# Tesseract is materially more accurate on larger glyphs; DVD subs are 720x480.
OCR_SCALE = 3
MIN_CUE_MS = 300
MAX_CUE_MS = 7000
# Below this many packets a track is signage/forced, not dialogue.
FORCED_TRACK_MAX_PACKETS = 50

_OCR_FIXES = [
    (re.compile(r"(?<![A-Za-z0-9])[|l](?=['’]|\s)"), "I"),  # bar/l misread as I
    (re.compile(r"(?<=[a-z])0(?=[a-z])"), "o"),
    (re.compile(r"[“”]"), '"'),
    (re.compile(r"[‘’]"), "'"),
    (re.compile(r"\s+"), " "),
]


def tesseract_path() -> Optional[str]:
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def subtitle_packet_counts(media_path: Path) -> dict[int, dict]:
    """Per-subtitle-stream packet counts, codec and language, in one pass."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "s", "-count_packets",
            "-show_entries", "stream=index,codec_name,nb_read_packets:stream_tags=language",
            "-of", "json", str(media_path),
        ],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        return {}
    out = {}
    for stream in json.loads(proc.stdout or "{}").get("streams", []):
        out[stream["index"]] = {
            "codec": stream.get("codec_name"),
            "packets": int(stream.get("nb_read_packets") or 0),
            "language": (stream.get("tags") or {}).get("language", "").lower(),
        }
    return out


def find_image_subtitle_stream(media_path: Path, language: str = "eng") -> Optional[int]:
    """Pick the fullest image-based subtitle track in the requested language.

    Selection is by packet count, not stream order: forced tracks share the
    same language tag and would otherwise win by appearing first.
    """
    streams = {
        index: info
        for index, info in subtitle_packet_counts(media_path).items()
        if info["codec"] in IMAGE_SUB_CODECS
    }
    if not streams:
        return None
    matching = {
        i: s for i, s in streams.items() if s["language"].startswith(language[:2])
    }
    pool = matching or streams
    best = max(pool, key=lambda i: pool[i]["packets"])
    if pool[best]["packets"] <= FORCED_TRACK_MAX_PACKETS:
        return None  # only signage tracks present; nothing worth OCR-ing
    return best


def subtitle_packet_times(media_path: Path, stream_index: int) -> list[tuple[int, int]]:
    """(start_ms, end_ms) for every packet in a subtitle stream."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", str(stream_index),
            "-show_entries", "packet=pts_time,duration_time",
            "-of", "csv=p=0", str(media_path),
        ],
        capture_output=True, text=True, errors="replace",
    )
    times: list[tuple[int, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(",")
        if not parts or not parts[0]:
            continue
        try:
            start = float(parts[0])
        except ValueError:
            continue
        try:
            duration = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
        except ValueError:
            duration = 0.0
        start_ms = int(start * 1000)
        end_ms = start_ms + int(duration * 1000 or MIN_CUE_MS)
        times.append((start_ms, end_ms))
    return times


def _clean(text: str) -> str:
    text = text.replace("\n", " ").strip()
    for pattern, replacement in _OCR_FIXES:
        text = pattern.sub(replacement, text)
    return text.strip(" -—")


def _prepare(png_path: Path):
    """Composite a subtitle bitmap for OCR; None if the frame is blank."""
    import cv2
    import numpy as np

    img = cv2.imread(str(png_path), cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim != 3 or img.shape[2] < 4:
        return None
    alpha = img[:, :, 3]
    if not (alpha > 0).any():
        return None  # sub2video "clear" frame: marks the end of the last cue

    mask = (alpha.astype(float) / 255)[..., None]
    on_black = (img[:, :, :3].astype(float) * mask).astype(np.uint8)
    inverted = 255 - cv2.cvtColor(on_black, cv2.COLOR_BGR2GRAY)
    return cv2.resize(
        inverted, None, fx=OCR_SCALE, fy=OCR_SCALE, interpolation=cv2.INTER_CUBIC
    )


def ocr_subtitle_stream(
    media_path: Path,
    stream_index: int,
    progress: Optional[Callable[[float, str], None]] = None,
) -> list[Cue]:
    """Render each subtitle bitmap and OCR it into a timed cue."""
    import cv2

    tess = tesseract_path()
    if not tess:
        raise RuntimeError(
            "tesseract not found — install it (winget install UB-Mannheim.TesseractOCR) "
            "to read image-based DVD subtitles"
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        # sub2video: using a subtitle stream as a filter input makes ffmpeg
        # rasterise it. Seeking breaks this, so render from the start.
        #
        # settb=1/1000 is load-bearing. -frame_pts writes the timestamp into
        # a 32-bit field; in the default microsecond timebase that overflows
        # at 2^31 us — 35.8 minutes — silently zeroing every timestamp in
        # the rest of the film. In milliseconds a 4-hour film still fits.
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y", "-i", str(media_path),
                "-filter_complex", f"[0:{stream_index}]settb=1/1000[o]", "-map", "[o]",
                "-fps_mode", "passthrough", "-frame_pts", "1",
                str(tmpdir / "sub_%09d.png"),
            ],
            capture_output=True, text=True, errors="replace",
        )
        images = sorted(tmpdir.glob("sub_*.png"))
        if not images:
            raise RuntimeError(
                f"no subtitle bitmaps rendered from stream {stream_index}:\n"
                f"{proc.stderr[-800:]}"
            )

        work = tmpdir / "ocr.png"
        cues: list[Cue] = []
        for n, image in enumerate(images, 1):
            try:
                start_ms = int(image.stem.split("_")[1])
            except (IndexError, ValueError):
                continue

            prepared = _prepare(image)
            if prepared is None:
                # sub2video emits a blank frame when a cue stops being shown
                if cues:
                    cues[-1].endMs = max(cues[-1].startMs + MIN_CUE_MS, start_ms)
                continue

            cv2.imwrite(str(work), prepared)
            out = subprocess.run(
                [tess, str(work), "stdout", "--psm", "6", "-l", "eng"],
                capture_output=True, text=True,
                # Tesseract emits UTF-8; the Windows locale codepage would
                # mangle every curly apostrophe into "â€™".
                encoding="utf-8", errors="replace",
            )
            text = _clean(out.stdout or "")
            if text:
                cues.append(Cue(start_ms, start_ms + MAX_CUE_MS, text))
            if progress and (n % 100 == 0 or n == len(images)):
                progress(n / len(images), f"OCR {n}/{len(images)} subtitle frames")

    for cue in cues:
        cue.endMs = min(max(cue.endMs, cue.startMs + MIN_CUE_MS), cue.startMs + MAX_CUE_MS)
    return cues
