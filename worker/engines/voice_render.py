"""Voice-only mute: drop the spoken word, keep the music and ambient.

A plain mute silences *all* audio for the flagged span, so a swear over
music leaves an audible hole. Voice-only mute instead source-separates the
audio into vocals + accompaniment (Demucs `htdemucs`, 2-stem), zeroes **only**
the vocals across the mute span, and keeps the accompaniment — music, Foley
and ambient play straight through; just the spoken word drops.

**Windowed.** Separation is expensive, so only a ~1 s pad around each finding
is run through Demucs, never the whole track. The padded window gives the
model context; only the finding's own span is actually replaced with the
vocals-removed mix. The rest of the audio is untouched.

Render-only: this produces a modified audio track, which the combined
renderer ([worker/render.py](worker/render.py)) feeds to FFmpeg in place of
the original audio. Jellyfin cannot apply it live.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..models import Segment
from .base import ProgressCb

#: Demucs `htdemucs` is trained at 44.1 kHz stereo; extract at that rate so no
#: resampling is needed before separation.
DEFAULT_SR = 44100
DEFAULT_CH = 2

#: Context padding, per side, fed to the separator around each finding. The
#: model separates better with a run-up; only the finding's span is replaced.
PAD_S = 1.0

#: A separator maps a window of audio (samples, channels) float32 in [-1, 1]
#: to its isolated vocals, same shape. Injectable so the splicing logic can be
#: tested without loading Demucs.
Separator = Callable[[np.ndarray, int], np.ndarray]

_MODEL = None


def _get_model():
    """Load (once) the Demucs htdemucs model. Lazy so importing this module —
    and running the tests, which inject a fake separator — never needs Demucs."""
    global _MODEL
    if _MODEL is None:
        try:
            from demucs.pretrained import get_model
        except ImportError as exc:  # pragma: no cover - env-specific
            raise RuntimeError(
                "voice-only mute needs Demucs: `uv add demucs` (downloads a "
                "~few-hundred-MB model on first use)"
            ) from exc
        _MODEL = get_model("htdemucs")
        _MODEL.eval()
    return _MODEL


def separate_vocals(window: np.ndarray, sr: int) -> np.ndarray:
    """Isolate the vocal stem of a short audio window with Demucs.

    ``window`` is (samples, channels) float32; returns the vocals in the same
    shape. Uses the standard Demucs normalise → separate → denormalise recipe,
    and the GPU when one is free, falling back to CPU on any CUDA error (the
    4 GB card is often busy with Ollama).
    """
    import torch
    from demucs.apply import apply_model

    model = _get_model()
    wav = torch.from_numpy(window.T).float()  # (channels, samples)
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)  # htdemucs expects stereo

    ref = wav.mean(0)
    wav = (wav - ref.mean()) / (ref.std() + 1e-8)

    def _apply(device: str):
        with torch.no_grad():
            return apply_model(
                model, wav[None].to(device), device=device, progress=False
            )[0]

    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        sources = _apply(device)
    except RuntimeError:
        sources = _apply("cpu")  # OOM / no free VRAM — CPU is slow but works

    sources = sources * ref.std() + ref.mean()
    vocals = sources[model.sources.index("vocals")]  # (channels, samples)
    out = vocals.cpu().numpy().T  # (samples, channels)
    return out[:, : window.shape[1]]


def remove_vocals(
    window: np.ndarray, sr: int, span0: int, span1: int, separate: Separator
) -> np.ndarray:
    """Return ``window`` with the vocals subtracted **only** across samples
    ``[span0, span1)``. Everything outside the span is returned untouched, so a
    caller can safely write back just the span.

    The separator sees the whole (padded) window for context; accompaniment is
    the mixture minus the isolated vocals, so music/ambient carry through.
    """
    vocals = separate(window, sr)
    out = window.copy()
    span1 = min(span1, len(window))
    if span1 > span0:
        out[span0:span1] = window[span0:span1] - vocals[span0:span1]
    return np.clip(out, -1.0, 1.0)


def _extract_pcm(
    media: Path,
    out_pcm: Path,
    sr: int,
    ch: int,
    start_s: Optional[float] = None,
    dur_s: Optional[float] = None,
) -> bool:
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start_s is not None:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(media)]
    if dur_s is not None:
        cmd += ["-t", f"{dur_s:.3f}"]
    cmd += ["-map", "0:a:0", "-ac", str(ch), "-ar", str(sr), "-f", "s16le", str(out_pcm)]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return proc.returncode == 0 and out_pcm.is_file()


def render_voice_removed_pcm(
    media: Path,
    segments: list[Segment],
    out_pcm: Path,
    progress: Optional[ProgressCb] = None,
    sr: int = DEFAULT_SR,
    ch: int = DEFAULT_CH,
    pad_s: float = PAD_S,
    separate: Optional[Separator] = None,
) -> Path:
    """Produce a full-length raw-PCM (s16le) audio track for ``media`` with the
    vocals removed across each of ``segments``' spans, everything else copied.

    Written as raw PCM so the renderer can hand it straight to FFmpeg as a
    second input. The full track is edited in place via a memory map, so only
    the ~1 s window around each finding is ever held as floats — the rest is
    never loaded, and only the finding's own span is rewritten.
    """
    separate = separate or separate_vocals
    if not _extract_pcm(media, out_pcm, sr, ch):
        raise RuntimeError(f"could not extract audio from {media}")

    pcm = np.memmap(out_pcm, dtype="<i2", mode="r+")
    frames = pcm.reshape(-1, ch)
    total = len(frames)
    pad = int(pad_s * sr)

    for i, seg in enumerate(segments):
        if progress:
            progress(i / max(1, len(segments)), f"removing voice {i + 1}/{len(segments)}")
        w0 = int(seg.startMs / 1000 * sr)
        w1 = int(seg.endMs / 1000 * sr)
        lo = max(0, w0 - pad)
        hi = min(total, w1 + pad)
        if hi <= lo:
            continue
        window = frames[lo:hi].astype(np.float32) / 32768.0
        span0, span1 = w0 - lo, w1 - lo
        out = remove_vocals(window, sr, span0, span1, separate)
        # Write back only the replaced span; untouched samples keep their exact
        # original quantisation (no needless float round-trip).
        s0, s1 = lo + span0, min(hi, lo + span1)
        frames[s0:s1] = np.round(out[span0 : span1] * 32767.0).astype("<i2")[: s1 - s0]

    pcm.flush()
    del pcm  # release the mmap so the file can be reopened by FFmpeg
    if progress:
        progress(1.0, "voice removed")
    return out_pcm


def voice_removed_window_wav(
    media: Path,
    start_s: float,
    dur_s: float,
    spans_s: list[tuple[float, float]],
    out_wav: Path,
    sr: int = DEFAULT_SR,
    ch: int = DEFAULT_CH,
    separate: Optional[Separator] = None,
) -> Optional[Path]:
    """A voice-removed WAV of one time window across **several** flagged spans.

    Extracts ``[start_s, start_s+dur_s)`` once, separates its vocals **once**,
    and subtracts them across each ``(lo, hi)`` of ``spans_s`` (absolute media
    time) — so a window with three voice findings still pays a single Demucs
    pass. Everything outside the spans plays through untouched. Returns None if
    extraction fails or there is nothing to remove.
    """
    separate = separate or separate_vocals
    import os
    import tempfile

    # mkstemp returns an OPEN fd; close it or Windows keeps the file locked and
    # the raw.unlink() below throws WinError 32 (the whole preview then fails).
    fd, raw_name = tempfile.mkstemp(suffix=".pcm")
    os.close(fd)
    raw = Path(raw_name)
    try:
        if not _extract_pcm(media, raw, sr, ch, start_s=start_s, dur_s=dur_s):
            return None
        data = np.fromfile(raw, dtype="<i2")
    finally:
        raw.unlink(missing_ok=True)

    if data.size == 0:
        return None
    window = data.reshape(-1, ch).astype(np.float32) / 32768.0
    total = len(window)
    spans = [
        (max(0, int((lo - start_s) * sr)), min(total, int((hi - start_s) * sr)))
        for lo, hi in spans_s
    ]
    spans = [(s0, s1) for s0, s1 in spans if s1 > s0]
    if not spans:
        return None

    # One separation for the whole window; zero the vocals across every span.
    vocals = separate(window, sr)
    out = window.copy()
    for s0, s1 in spans:
        out[s0:s1] = window[s0:s1] - vocals[s0:s1]
    out = np.clip(out, -1.0, 1.0)
    pcm16 = np.round(out * 32767.0).astype("<i2")

    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())
    return out_wav


def voice_removed_wav(
    media: Path,
    start_s: float,
    dur_s: float,
    span_lo_s: float,
    span_hi_s: float,
    out_wav: Path,
    sr: int = DEFAULT_SR,
    ch: int = DEFAULT_CH,
    separate: Optional[Separator] = None,
) -> Optional[Path]:
    """A voice-removed WAV of one time window, for the review preview.

    Extracts ``[start_s, start_s+dur_s)``, removes the vocals across
    ``[span_lo_s, span_hi_s)`` (absolute media time), and writes a WAV the
    review clip can mux over the video. Returns None if extraction fails.
    """
    return voice_removed_window_wav(
        media, start_s, dur_s, [(span_lo_s, span_hi_s)], out_wav, sr, ch, separate
    )
