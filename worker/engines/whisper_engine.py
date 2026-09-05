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
from .profanity import Hit, is_profane, merge_hits, resolve_flags

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

    def _extract_audio(self, media_path: Path, progress: ProgressCb) -> Path:
        """Pre-extract the default audio track to a local mono 16kHz WAV.

        faster-whisper decodes straight from the source container via PyAV,
        and a sequential decode can stop short partway through even against
        an already-local, byte-verified copy. Measured on one film: a plain
        ffmpeg extraction (also sequential, from the same local copy) hit the
        identical wall — but seeking straight to just past that timestamp and
        decoding from there read cleanly, on every audio track. That is the
        signature of a mid-file timestamp discontinuity, not corruption at
        that instant: a DVD rip that spliced extra content into the main
        feature is the usual cause. A fresh seek reinitializes the demuxer's
        state instead of carrying the bad state forward, so a sequential
        read that comes up short is followed by one more pass starting where
        it broke, then the two are stitched onto one continuous timeline
        (``_stitch_past_break``) rather than retried as-is — retrying the
        same sequential read only reproduces the same break.
        """
        import subprocess

        expected_s = media_duration(media_path)
        wav_path = media_path.with_name(media_path.stem + ".cleanmedia-audio.wav")

        def ffmpeg_extract(dst: Path, start_s: float = 0.0) -> None:
            cmd = ["ffmpeg", "-v", "error", "-y"]
            if start_s:
                cmd += ["-ss", f"{start_s:.3f}"]
            cmd += [
                "-i", str(media_path),
                "-map", "0:a:0", "-vn", "-sn",
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                str(dst),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg audio extraction failed: {proc.stderr.strip()}"
                )

        def stitch_past_break(first_s: float) -> None:
            remainder = wav_path.with_name(wav_path.stem + ".part2.wav")
            ffmpeg_extract(remainder, start_s=first_s)
            stitched = wav_path.with_name(wav_path.stem + ".stitched.wav")
            concat_list = wav_path.with_name(wav_path.stem + ".concat.txt")
            # Reference inputs by bare filename with cwd set to their folder,
            # not full paths — the concat demuxer's list format doesn't need
            # to deal with Windows drive letters/backslashes that way.
            concat_list.write_text(
                f"file '{wav_path.name}'\nfile '{remainder.name}'\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list), "-c", "copy", str(stitched),
                ],
                cwd=str(wav_path.parent), capture_output=True, text=True,
            )
            remainder.unlink(missing_ok=True)
            concat_list.unlink(missing_ok=True)
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg concat failed: {proc.stderr.strip()}")
            wav_path.unlink(missing_ok=True)
            stitched.rename(wav_path)

        def run() -> Path:
            ffmpeg_extract(wav_path)
            got_s = media_duration(wav_path)
            if expected_s and got_s < expected_s * 0.98:
                progress(0.0, f"decode broke at {got_s:.0f}s — stitching past it")
                stitch_past_break(got_s)
                got_s = media_duration(wav_path)
                if expected_s and got_s < expected_s * 0.98:
                    raise RuntimeError(
                        f"extracted only {got_s:.0f}s of audio from a "
                        f"{expected_s:.0f}s film, even after stitching past "
                        "one break"
                    )
            return wav_path

        progress(0.0, "extracting audio track")
        return retry_media_read(
            run,
            transient=(RuntimeError,),
            on_retry=lambda exc: progress(0.0, f"retrying audio extraction: {exc}"),
        )

    def _transcribe(
        self,
        audio_path: Path,
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
        #
        # `audio_path` is already a plain WAV extracted by ffmpeg
        # (_extract_audio) rather than the source film — PyAV's demux of the
        # WAV is trivial, so a short decode here means the *WAV* was cut
        # short (a local-disk hiccup, not a share drop). Kept as a cheap
        # invariant check either way: this project's recurring failure mode
        # is a step that reports success while covering only part of the
        # film. The exception classes below still matter for that: a dropped
        # read can surface as PyAV's av.error.ArgumentError (EINVAL 22),
        # which is a *ValueError*, not an OSError — a filter of only
        # (OSError, RuntimeError) never matches it, so real drops sail
        # through with zero retries. Catch av.error.FFmpegError (the base of
        # ArgumentError and PyAV's other I/O errors) alongside OSError
        # (ENOENT 2) and RuntimeError (faster-whisper's own wrapping). A
        # genuinely broken WAV fails every attempt and the last error
        # surfaces unchanged.
        import av.error

        expected_s = media_duration(audio_path)

        def run():
            segments, info = model.transcribe(
                str(audio_path),
                word_timestamps=True,
                vad_filter=vad_filter,
                beam_size=5,
            )
            if expected_s and info.duration and info.duration < expected_s * 0.98:
                raise TransientMediaRead(
                    f"decoded only {info.duration:.0f}s of a {expected_s:.0f}s "
                    "audio track, retrying"
                )
            return segments, info

        segments, info = retry_media_read(
            run,
            transient=(OSError, RuntimeError, av.error.FFmpegError),
            on_retry=lambda exc: progress(0.01, f"retrying after read error: {exc}"),
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
        include_mild, include_blasphemy, extra = resolve_flags(options)

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
                audio_path = self._extract_audio(decode_path, progress)
                try:
                    transcript = self._transcribe(
                        audio_path,
                        model_name,
                        progress,
                        vad_filter=bool(options.get("vadFilter", False)),
                    )
                finally:
                    audio_path.unlink(missing_ok=True)
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
