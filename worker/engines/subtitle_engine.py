"""Subtitle profanity adapter.

Reads the media's own English subtitle track (embedded or sidecar SRT) and
flags profanity for muting. Subtitles are human-transcribed, so this catches
lines ASR loses under music and explosions.

Subtitle cues have no per-word timings, so the mute window is narrowed to
the individual word in four escalating ways, from most to least exact:

0. if the cue is a single word, the human already timed the whole cue to
   that word — its bounds ARE the word timing, exact and free;
1. a cached Whisper transcript word inside the cue window, if one matches;
2. otherwise a targeted re-transcription of just that cue's audio with the
   voice-activity filter off — this recovers words ASR skipped in the
   full-movie pass, which is exactly where profanity tends to hide;
3. otherwise the word's position estimated from its character offset.

Matching is fuzzy: whisper writes possessives and inflections the subtitle
does not ("god's" for "God", "asses" for "ass"), and an exact string test
threw those away to the estimate tier. A found word's span is clamped rather
than rejected for being padded-long, and the window is always clamped inside
the cue, so a bad estimate can never mute neighbouring dialogue.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from ..models import Segment, Timeline
from .base import EngineAdapter, ProgressCb
from .mute_render import render_muted
from .profanity import Hit, is_profane, merge_hits, normalize
from .subtitles import (
    Cue,
    extract_srt,
    external_srt,
    find_text_subtitle_stream,
    parse_srt,
    write_srt,
)
from .vobsub import find_image_subtitle_stream, ocr_subtitle_stream

# A small cushion so the word's edges are not clipped, without reaching into
# the words beside it. 200ms each side plus a 400ms floor put a 0.3s word
# inside an 0.8s mute, which in fast dialogue swallowed its neighbours.
PAD_MS = 70
MIN_WINDOW_MS = 240
# No single spoken word runs longer than this; a wider ASR span means the
# model merged neighbouring speech and the timing cannot be trusted.
MAX_WORD_MS = 1500
# Audio pulled around a cue when re-transcribing for precise word timing.
CLIP_MARGIN_MS = 1500
# How far outside its cue an ASR-derived timing may sit before we distrust it.
DRIFT_TOLERANCE_MS = 1000


class SubtitleEngine(EngineAdapter):
    name = "subtitles"

    def version(self) -> str:
        return "1.0"

    def health(self) -> dict[str, Any]:
        import shutil

        ok = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        return {"available": ok} if ok else {
            "available": False,
            "error": "ffmpeg/ffprobe not on PATH",
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "categories": ["profanity"],
            "actions": ["mute"],
            "options": {
                "language": "subtitle language code (default eng)",
                "includeMild": "bool (also flag hell/damn/ass/etc, default false)",
                "includeBlasphemy": "bool (also flag God/Jesus/Christ as exclamations)",
                "extraWords": "list of custom words to flag",
                "wholeCue": "bool — mute the entire subtitle cue instead of the word",
                "preciseTiming": "bool — re-transcribe cue audio for exact word timing (default true)",
                "timingModel": "ASR model used for precise timing; "
                "'nyrahealth/faster_CrisperWhisper' gives sharper word boundaries",
            },
        }

    def _load_cues(
        self, media_path: Path, language: str, progress: ProgressCb
    ) -> tuple[list[Cue], Path]:
        sidecar = external_srt(media_path)
        if sidecar:
            progress(0.2, f"using subtitle file {sidecar.name}")
            return parse_srt(sidecar), sidecar

        progress(0.1, "looking for embedded subtitles")
        stream = find_text_subtitle_stream(media_path, language)
        if stream is not None:
            out = media_path.with_name(f"{media_path.stem}.{language}.srt")
            progress(0.2, f"extracting subtitle stream {stream}")
            extract_srt(media_path, stream, out)
            return parse_srt(out), out

        # Most DVD rips carry only bitmap subtitles. OCR still beats ASR on
        # them, because the text was transcribed by a human.
        image_stream = find_image_subtitle_stream(media_path, language)
        if image_stream is None:
            raise RuntimeError(
                "no subtitle track found; supply a sidecar .srt or use the "
                "whisper engine"
            )
        progress(0.15, f"OCR-ing image subtitle stream {image_stream}")
        cues = ocr_subtitle_stream(
            media_path,
            image_stream,
            lambda frac, stage: progress(0.15 + frac * 0.25, stage),
        )
        out = media_path.with_name(f"{media_path.stem}.{language}.srt")
        write_srt(cues, out)
        progress(0.4, f"OCR produced {len(cues)} cues -> {out.name}")
        return cues, out

    @staticmethod
    def _word_matches(heard: str, target: str) -> bool:
        """Does an ASR-heard word correspond to the target profanity?

        Whisper writes possessives and inflections the subtitle does not —
        "god's" for "God", "asses" for "ass", "damnit" for "damn". An exact
        string test threw all of those to the estimate tier even though the
        timing was right there. Both are already normalised (lowercase, outer
        punctuation stripped, apostrophes kept)."""
        if heard == target:
            return True
        # drop a trailing possessive/plural 's from either side
        h = heard[:-2] if heard.endswith("'s") else heard
        t = target[:-2] if target.endswith("'s") else target
        if h == t:
            return True
        # one is a prefix of the other, with enough shared length to be safe
        short, long = sorted((h, t), key=len)
        return len(short) >= 3 and long.startswith(short)

    def _clamp_span(
        self, cue: Cue, start_ms: int, end_ms: int, source: str
    ) -> Optional[tuple[int, int, str]]:
        """Accept a found word by its start, clamping a padded duration.

        A word whisper timed at 2.4s is not implausible — it padded trailing
        silence. Trust the start (whisper places it more accurately than the
        cue does) and cap the length, rather than discarding real timing."""
        if not (cue.startMs - CLIP_MARGIN_MS <= start_ms <= cue.endMs + CLIP_MARGIN_MS):
            return None
        end_ms = min(end_ms, start_ms + MAX_WORD_MS)
        if end_ms <= start_ms:
            end_ms = start_ms + MIN_WINDOW_MS
        return start_ms, end_ms, source

    def _from_cache(
        self, cue: Cue, key: str, cached: dict[str, list[dict]]
    ) -> Optional[tuple[int, int, str]]:
        """A cached ASR word matching the target and landing inside the cue."""
        lo = cue.startMs - CLIP_MARGIN_MS
        hi = cue.endMs + CLIP_MARGIN_MS
        mid = (cue.startMs + cue.endMs) // 2
        best = None
        for norm, words in cached.items():
            if not self._word_matches(norm, key):
                continue
            for w in words:
                start_ms = int(w["start"] * 1000)
                if lo <= start_ms <= hi:
                    dist = abs(start_ms - mid)  # nearest the cue centre
                    if best is None or dist < best[0]:
                        best = (dist, start_ms, int(w["end"] * 1000))
        return self._clamp_span(cue, best[1], best[2], "cached-asr") if best else None

    def _retranscribe(
        self, media_path: Path, cue: Cue, key: str, model
    ) -> Optional[tuple[int, int, str]]:
        """Re-transcribe just this cue's audio to recover an exact word timing."""
        start_s = max(0, cue.startMs - CLIP_MARGIN_MS) / 1000
        dur_s = (cue.endMs - cue.startMs + 2 * CLIP_MARGIN_MS) / 1000
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            proc = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-y", "-ss", f"{start_s:.3f}",
                    "-i", str(media_path), "-t", f"{dur_s:.3f}",
                    "-vn", "-ac", "1", "-ar", "16000", str(wav),
                ],
                capture_output=True, text=True, errors="replace",
            )
            if proc.returncode != 0 or not wav.exists():
                return None
            segments, _ = model.transcribe(
                str(wav), word_timestamps=True, vad_filter=False, beam_size=5
            )
            mid = (cue.startMs + cue.endMs) // 2
            best = None
            for seg in segments:
                for w in seg.words or []:
                    if self._word_matches(normalize(w.word), key):
                        start_ms = int((start_s + w.start) * 1000)
                        end_ms = int((start_s + w.end) * 1000)
                        dist = abs(start_ms - mid)  # nearest the cue centre
                        if best is None or dist < best[0]:
                            best = (dist, start_ms, end_ms)
        return (
            self._clamp_span(cue, best[1], best[2], "retranscribed") if best else None
        )

    def _estimate(self, cue: Cue, char_offset: int, word_len: int) -> tuple[int, int, str]:
        """Position the word by its character offset within the cue text."""
        span = max(cue.endMs - cue.startMs, MIN_WINDOW_MS)
        total = max(len(cue.text), 1)
        start = cue.startMs + int(span * char_offset / total)
        width = max(int(span * word_len / total), MIN_WINDOW_MS)
        return start, start + width, "estimated"

    def analyze(
        self,
        media_path: Path,
        fingerprint: str,
        options: dict[str, Any],
        progress: ProgressCb,
    ) -> tuple[Timeline, Optional[Path]]:
        language = options.get("language", "eng")
        include_mild = bool(options.get("includeMild", False))
        include_blasphemy = bool(options.get("includeBlasphemy", False))
        extra = {w.lower() for w in options.get("extraWords", [])}
        whole_cue = bool(options.get("wholeCue", False))
        precise = bool(options.get("preciseTiming", True)) and not whole_cue

        cues, srt_path = self._load_cues(media_path, language, progress)
        progress(0.4, f"scanning {len(cues)} subtitle cues")

        # Precise timings from a cached Whisper transcript, where it has them.
        by_word: dict[str, list[dict]] = {}
        transcript_path = media_path.with_name(media_path.stem + ".whisper.json")
        if transcript_path.exists():
            data = json.loads(transcript_path.read_text(encoding="utf-8"))
            for seg in data.get("segments", []):
                for w in seg.get("words", []):
                    by_word.setdefault(normalize(w["word"]), []).append(w)

        # Find matches first, so we only load the ASR model if needed.
        matches: list[tuple[Cue, str, int, int]] = []
        for cue in cues:
            offset = 0
            for raw in cue.text.split():
                idx = cue.text.find(raw, offset)
                offset = idx + len(raw) if idx >= 0 else offset
                if is_profane(raw, include_mild, extra, include_blasphemy):
                    matches.append((cue, normalize(raw), max(idx, 0), len(raw)))

        model = None
        if precise and matches:
            # Load whisper for anything the cache cannot already time — including
            # single-word cues, whose display duration is wider than the word is
            # spoken, so ASR gives a tighter mute than the cue bounds do.
            unresolved = [
                m for m in matches if self._from_cache(m[0], m[1], by_word) is None
            ]
            if unresolved:
                try:
                    from faster_whisper import WhisperModel

                    from .whisper_engine import DEFAULT_MODEL

                    name = options.get("timingModel", DEFAULT_MODEL)
                    progress(
                        0.5, f"loading {name} to time {len(unresolved)} word(s)"
                    )
                    try:
                        model = WhisperModel(
                            name, device="cuda", compute_type="int8_float16"
                        )
                    except Exception:
                        model = WhisperModel(name, device="cpu", compute_type="int8")
                except ImportError:
                    model = None

        hits: list[Hit] = []
        sources: list[str] = []
        for n, (cue, key, char_offset, word_len) in enumerate(matches, 1):
            if whole_cue:
                start, end, source = cue.startMs, cue.endMs, "whole-cue"
            else:
                # Prefer the tight, to-the-word ASR span for every cue.
                resolved = self._from_cache(cue, key, by_word)
                if resolved is None and model is not None:
                    progress(
                        0.5 + 0.45 * n / len(matches),
                        f"timing '{key}' at {cue.startMs // 1000}s",
                    )
                    resolved = self._retranscribe(media_path, cue, key, model)
                if resolved is None:
                    if len(cue.text.split()) == 1:
                        # ASR could not place it, but the cue is nothing but
                        # this word, so its bounds are a safe exact fallback —
                        # wider than the word, yet never another word.
                        start, end, source = cue.startMs, cue.endMs, "single-word-cue"
                    else:
                        start, end, source = self._estimate(cue, char_offset, word_len)
                else:
                    start, end, source = resolved
                start = max(cue.startMs - DRIFT_TOLERANCE_MS, start)
                end = min(
                    max(end, start + MIN_WINDOW_MS), cue.endMs + DRIFT_TOLERANCE_MS
                )
            sources.append(source)
            hits.append(
                Hit(
                    startMs=max(0, start - PAD_MS),
                    endMs=end + PAD_MS,
                    word=key,
                    confidence=1.0 if source != "estimated" else 0.5,
                    context=f"{cue.text[:110]} ({source})",
                )
            )

        merged = merge_hits(hits)
        progress(0.95, f"{len(hits)} words in {len(merged)} segments")

        timeline = Timeline(
            mediaFingerprint=fingerprint,
            segments=[
                Segment(
                    id=i + 1,
                    startMs=h.startMs,
                    endMs=h.endMs,
                    category="profanity",
                    confidence=h.confidence,
                    engine=self.name,
                    recommendedAction="mute",
                    approved=None,
                    reasoning=f"[{h.word}] {h.context}",
                )
                for i, h in enumerate(merged)
            ],
        )
        progress(1.0, "subtitle scan complete")
        return timeline, srt_path

    def render(
        self,
        media_path: Path,
        plan_path: Path,
        timeline: Timeline,
        output_path: Path,
        progress: ProgressCb,
    ) -> Path:
        return render_muted(media_path, timeline, output_path, progress)
