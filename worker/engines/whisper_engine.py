"""Whisper profanity adapter.

Transcribes the media with faster-whisper (word-level timestamps), matches
words against a profanity list, and emits `mute` segments in the standard
timeline. Rendering mutes approved segments with FFmpeg; the video stream is
stream-copied so the original picture is untouched.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any, Optional

from ..models import Segment, Timeline
from ..retry import retry_media_read
from ..shots import TransientMediaRead, media_duration
from ..staging import local_media
from .base import EngineAdapter, ProgressCb
from .mute_render import render_muted
from .profanity import Hit, is_profane, merge_hits

PAD_MS = 150
DEFAULT_MODEL = "medium.en"

# CrisperWhisper publishes much better word-boundary scores than stock
# Whisper (Common Voice F1 0.80 vs 0.42), but measured on this project's
# actual workload — 3-5s movie clips with music and effects under the
# dialogue — the faster-whisper conversion degrades badly, returning filler
# ("um hmm the would right the") where medium.en transcribes correctly, and
# repeating words. Its DTW-based alignment, the part that earns those
# scores, lives in the authors' patched transformers fork and does not
# survive the CTranslate2 conversion. Selectable, but not recommended here.
# License is CC-BY-NC-4.0: fine for personal use, not commercial.
CRISPER_MODEL = "nyrahealth/faster_CrisperWhisper"


class WhisperEngine(EngineAdapter):
    name = "whisper"

    def version(self) -> str:
        try:
            return f"faster-whisper {pkg_version('faster-whisper')}"
        except PackageNotFoundError:
            return "not installed"

    def health(self) -> dict[str, Any]:
        try:
            pkg_version("faster-whisper")
            return {"available": True, "version": self.version()}
        except PackageNotFoundError:
            return {"available": False, "error": "faster-whisper not installed"}

    def capabilities(self) -> dict[str, Any]:
        return {
            "categories": ["profanity"],
            "actions": ["mute"],
            "options": {
                "model": ["medium.en", "small.en", "large-v3", CRISPER_MODEL],
                "includeMild": "bool (also flag hell/damn/etc, default false)",
                "includeBlasphemy": "bool (also flag God/Jesus/Christ as exclamations)",
                "extraWords": "list of custom words to flag",
                "vadFilter": "bool — voice-activity filter; default false because "
                "it drops speech under music and loses profanity",
            },
        }

    def _transcript_path(self, media_path: Path) -> Path:
        return media_path.with_name(media_path.stem + ".whisper.json")

    def _transcribe(
        self,
        media_path: Path,
        model_name: str,
        progress: ProgressCb,
        vad_filter: bool = False,
    ) -> dict:
        from faster_whisper import WhisperModel

        progress(0.0, f"loading whisper model {model_name}")
        try:
            model = WhisperModel(model_name, device="cuda", compute_type="int8_float16")
            device = "cuda"
        except Exception:
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            device = "cpu"
        progress(0.01, f"transcribing on {device}")

        # VAD off by default: with it on, faster-whisper discards speech
        # buried under music and effects, transcribing those windows as
        # silence.
        #
        # Measured against one film's own subtitle track, ASR found 4
        # of 9 profanity instances. Of the 5 misses, 3 were transcribed as
        # pure silence (VAD, fixed by this flag); the remaining 2 were
        # transcription failures VAD cannot explain — one hallucinated a
        # different sentence, the other truncated the line immediately
        # before the profanity. So expect this to help substantially but not
        # to close the gap: a human-written subtitle track got 9 of 9, which
        # is why the subtitle engine is preferred wherever one exists.
        # faster-whisper decodes the whole audio up front (via PyAV) inside this
        # call — before transcribe() returns — so a flaky-share read that drops
        # mid-decode surfaces right here, and a retry re-reads the file cleanly.
        #
        # The exception *class* is the trap this originally got wrong: a dropped
        # SMB read raises PyAV's av.error.ArgumentError (EINVAL 22), which is a
        # *ValueError*, not an OSError — so a filter of only (OSError, RuntimeError)
        # never matched it, and every drop failed the job with zero retries. It
        # bit only the largest films, whose full read runs long enough to reliably
        # hit a drop. Catch av.error.FFmpegError (the base of ArgumentError and
        # PyAV's other I/O errors); keep OSError for ENOENT 2 and RuntimeError for
        # faster-whisper's own wrapping. A genuinely broken file fails every
        # attempt and the last error surfaces unchanged. A full-film read off this
        # share drops most of the time, so give it more attempts than the default.
        import av.error

        expected_s = media_duration(media_path)

        def run():
            segments, info = model.transcribe(
                str(media_path),
                word_timestamps=True,
                vad_filter=vad_filter,
                beam_size=5,
            )
            # PyAV raises on a dropped decode, but guard the one case it might
            # not: a partial read that ends like a clean EOF would silently
            # transcribe only the film's opening and report it "clean" — the
            # invariant-not-exit-code failure this project keeps hitting. The
            # container duration is a cheap header read (reliable even when the
            # body decode is not); a short decode is a dropped read, so retry it.
            if expected_s and info.duration and info.duration < expected_s * 0.98:
                raise TransientMediaRead(
                    f"decoded only {info.duration:.0f}s of a {expected_s:.0f}s "
                    "film — dropped share read, retrying"
                )
            return segments, info

        segments, info = retry_media_read(
            run,
            transient=(OSError, RuntimeError, av.error.FFmpegError),
            on_retry=lambda exc: progress(0.01, f"retrying after read error: {exc}"),
            backoffs=(3.0, 6.0, 9.0, 12.0, 15.0, 20.0, 30.0),
        )
        duration = info.duration or 1.0

        out_segments = []
        for seg in segments:
            frac = min(seg.end / duration, 1.0)
            progress(
                0.01 + frac * 0.9,
                f"transcribing {int(seg.end // 60)}m/{int(duration // 60)}m",
            )
            out_segments.append(
                {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text.strip(),
                    "words": [
                        {
                            "start": w.start,
                            "end": w.end,
                            "word": w.word,
                            "probability": w.probability,
                        }
                        for w in (seg.words or [])
                    ],
                }
            )
        return {"model": model_name, "duration": duration, "segments": out_segments}

    def analyze(
        self,
        media_path: Path,
        fingerprint: str,
        options: dict[str, Any],
        progress: ProgressCb,
    ) -> tuple[Timeline, Optional[Path]]:
        import json

        model_name = options.get("model", DEFAULT_MODEL)
        include_mild = bool(options.get("includeMild", False))
        include_blasphemy = bool(options.get("includeBlasphemy", False))
        extra = {w.lower() for w in options.get("extraWords", [])}

        # Transcription is the expensive part — cache it as a sidecar so
        # wordlist changes only re-match, never re-transcribe.
        transcript_path = self._transcript_path(media_path)
        transcript = None
        if transcript_path.exists() and not options.get("forceTranscribe"):
            cached = json.loads(transcript_path.read_text(encoding="utf-8"))
            if cached.get("model") == model_name:
                transcript = cached
                progress(0.9, "using cached transcript")
        if transcript is None:
            # Decode from a local copy when the media is a large file on the
            # flaky share: a full-film read there drops before it finishes and a
            # from-scratch retry may never get a clean pass, so stage it first
            # (worker/staging.py). The transcript sidecar stays keyed to the
            # original media_path; only the decode reads the staged copy.
            with local_media(media_path, progress) as decode_path:
                transcript = self._transcribe(
                    decode_path,
                    model_name,
                    progress,
                    vad_filter=bool(options.get("vadFilter", False)),
                )
            transcript_path.write_text(
                json.dumps(transcript, indent=1), encoding="utf-8"
            )

        hits: list[Hit] = []
        for seg in transcript["segments"]:
            for word in seg["words"]:
                if is_profane(word["word"], include_mild, extra, include_blasphemy):
                    hits.append(
                        Hit(
                            startMs=max(0, int(word["start"] * 1000) - PAD_MS),
                            endMs=int(word["end"] * 1000) + PAD_MS,
                            word=word["word"].strip(),
                            confidence=float(word["probability"]),
                            context=seg["text"][:120],
                        )
                    )

        merged = merge_hits(hits)
        progress(0.98, f"{len(hits)} words in {len(merged)} segments")

        timeline = Timeline(
            mediaFingerprint=fingerprint,
            segments=[
                Segment(
                    id=i + 1,
                    startMs=h.startMs,
                    endMs=h.endMs,
                    category="profanity",
                    confidence=round(h.confidence, 3),
                    engine=self.name,
                    recommendedAction="mute",
                    approved=None,
                    reasoning=f"[{h.word}] {h.context}",
                )
                for i, h in enumerate(merged)
            ],
        )
        progress(1.0, "transcription complete")
        return timeline, transcript_path

    def render(
        self,
        media_path: Path,
        plan_path: Path,
        timeline: Timeline,
        output_path: Path,
        progress: ProgressCb,
    ) -> Path:
        """Mute all segments not explicitly rejected. Video is stream-copied."""
        return render_muted(media_path, timeline, output_path, progress)
