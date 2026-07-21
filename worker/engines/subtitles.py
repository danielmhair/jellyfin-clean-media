"""Subtitle discovery and SRT parsing.

Embedded text subtitles are ground truth for dialogue — far more reliable
than ASR for catching profanity, since they are transcribed by humans and
do not degrade during loud action scenes.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt"}
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")
# SDH sound cues and speaker labels: "(SIGHS)", "[MUSIC]", "TONY:"
_CUE_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]|^[A-Z][A-Z '\.]{1,20}:")


@dataclass
class Cue:
    startMs: int
    endMs: int
    text: str


def find_text_subtitle_stream(media_path: Path, language: str = "eng") -> Optional[int]:
    """Return the ffmpeg stream index of the best text subtitle track."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language,title",
            "-of", "json", str(media_path),
        ],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        return None
    streams = json.loads(proc.stdout or "{}").get("streams", [])
    candidates = [s for s in streams if s.get("codec_name") in TEXT_SUB_CODECS]
    if not candidates:
        return None
    for s in candidates:
        if (s.get("tags") or {}).get("language", "").lower().startswith(language[:2]):
            return s["index"]
    return candidates[0]["index"]


def extract_srt(media_path: Path, stream_index: int, out_path: Path) -> Path:
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(media_path),
            "-map", f"0:{stream_index}", "-c:s", "srt", str(out_path),
        ],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"subtitle extraction failed:\n{proc.stderr[-1000:]}")
    return out_path


def _ts_to_ms(stamp: str) -> int:
    h, m, rest = stamp.strip().split(":")
    s, ms = rest.replace(".", ",").split(",")
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 bytes that were decoded as cp1252 ("Godâ€™s" -> "God's").

    Older OCR runs wrote SRTs through the Windows codepage. Rather than
    force a re-OCR of every cached file, repair it on read.
    """
    if "â€" not in text and "Ã" not in text:
        return text
    try:
        return text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def parse_srt(path: Path, strip_sound_cues: bool = True) -> list[Cue]:
    raw = repair_mojibake(path.read_text(encoding="utf-8", errors="replace"))
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start, end = lines[1].split("-->")
        text = " ".join(lines[2:])
        text = _TAG_RE.sub("", text)
        if strip_sound_cues:
            text = _CUE_RE.sub(" ", text)
        text = " ".join(text.split())
        if text:
            cues.append(Cue(_ts_to_ms(start), _ts_to_ms(end), text))
    return cues


def _ms_to_ts(ms: int) -> str:
    h, rem = divmod(max(ms, 0), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue], path: Path) -> Path:
    """Persist cues as SRT so an OCR pass is done once, then cached."""
    blocks = [
        f"{i}\n{_ms_to_ts(c.startMs)} --> {_ms_to_ts(c.endMs)}\n{c.text}\n"
        for i, c in enumerate(cues, 1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def external_srt(media_path: Path) -> Optional[Path]:
    for suffix in (".en.srt", ".eng.srt", ".srt"):
        candidate = media_path.with_name(media_path.stem + suffix)
        if candidate.exists():
            return candidate
    return None
