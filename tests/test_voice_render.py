"""Voice-only mute: the splicing logic and the renderer wiring.

Demucs is not exercised here — it needs a model download and is slow. The
separation is injected as a fake, so these tests cover exactly the code this
project owns: which samples get replaced, and how the voice-removed track is
wired into the FFmpeg command.
"""

from pathlib import Path

import numpy as np

from worker.engines.voice_render import remove_vocals, render_voice_removed_pcm
from worker.models import Segment, Timeline
from worker.render import build_command


def _seg(i, start, end, action, approved=True):
    return Segment(
        id=i, startMs=start, endMs=end, category="x", confidence=1.0,
        engine="e", recommendedAction=action, approved=approved,
    )


def _tl(*segs):
    return Timeline(mediaFingerprint="fp", segments=list(segs))


# The vocal stem the fake separator "isolates": the whole window. So removing
# vocals across a span drives that span to ~0, and the rest is untouched.
def _all_is_vocal(window, sr):
    return window.copy()


def test_remove_vocals_only_touches_the_span():
    window = np.ones((100, 2), dtype=np.float32) * 0.5
    out = remove_vocals(window, 44100, 30, 70, _all_is_vocal)
    # Outside the span: original, untouched.
    assert np.allclose(out[:30], 0.5)
    assert np.allclose(out[70:], 0.5)
    # Inside the span: mixture minus (all-of-it) vocals ≈ 0.
    assert np.allclose(out[30:70], 0.0)


def test_remove_vocals_keeps_accompaniment():
    # Mixture = music (0.3) + voice (0.4). The separator returns just the voice;
    # removing it must leave the music, not silence.
    window = np.full((50, 2), 0.7, dtype=np.float32)
    voice = np.full((50, 2), 0.4, dtype=np.float32)
    out = remove_vocals(window, 44100, 10, 40, lambda w, sr: voice)
    assert np.allclose(out[10:40], 0.3)  # music plays through
    assert np.allclose(out[:10], 0.7)


def test_render_voice_removed_pcm_splices_only_the_span(tmp_path, monkeypatch):
    sr, ch = 44100, 2
    # 2 s of full-scale tone, as the on-disk s16le the extractor would produce.
    samples = np.full((2 * sr, ch), 16000, dtype="<i2")
    src = tmp_path / "audio.pcm"
    samples.tofile(src)

    # Stand in for the ffmpeg extract: copy our synthetic PCM to the target.
    def fake_extract(media, out_pcm, s, c, start_s=None, dur_s=None):
        Path(out_pcm).write_bytes(src.read_bytes())
        return True

    monkeypatch.setattr(
        "worker.engines.voice_render._extract_pcm", fake_extract
    )

    out = tmp_path / "voiced.pcm"
    # Finding at 1.0–1.2 s; pad_s=0.1 so the window is 0.9–1.3 s.
    seg = _seg(1, 1000, 1200, "voice")
    render_voice_removed_pcm(
        Path("movie.mkv"), [seg], out, sr=sr, ch=ch, pad_s=0.1,
        separate=_all_is_vocal,
    )

    result = np.fromfile(out, dtype="<i2").reshape(-1, ch)
    lo, hi = int(1.0 * sr), int(1.2 * sr)
    # The flagged span is driven to silence...
    assert np.abs(result[lo:hi]).max() <= 1
    # ...and everything on either side is byte-for-byte the original.
    assert np.array_equal(result[:lo], samples[:lo])
    assert np.array_equal(result[hi:], samples[hi:])


def test_voice_action_feeds_second_audio_input():
    cmd, n_blur, n_mute = build_command(
        Path("in.mkv"), _tl(_seg(1, 1000, 2000, "voice")), Path("out.mkv"),
        audio_path=Path("voiced.pcm"), audio_sr=44100, audio_ch=2,
    )
    assert (n_blur, n_mute) == (0, 0)  # voice is neither a blur nor a hard mute
    # A second, raw-PCM input, mapped in place of the source audio.
    assert cmd.count("-i") == 2
    assert "voiced.pcm" in cmd
    assert "-map" in cmd and "1:a:0" in cmd
    assert "0:a:0" not in cmd
    # A voice-removed track is a new stream, so audio must be re-encoded.
    assert cmd[cmd.index("-c:a") + 1] == "ac3"
    # Video is untouched, so it is stream-copied.
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_voice_only_needs_no_duration():
    # Only skips shorten the film; a voice-only render must not demand duration.
    cmd, _, _ = build_command(
        Path("in.mkv"), _tl(_seg(1, 1000, 2000, "voice")), Path("out.mkv"),
        audio_path=Path("voiced.pcm"), audio_sr=44100, audio_ch=2,
    )
    assert cmd  # did not raise


def test_voice_counts_as_renderable():
    # A timeline whose only approved finding is voice-only is still a render.
    from worker.render import approved_for_render

    tl = _tl(_seg(1, 1000, 2000, "voice", approved=True))
    assert len(approved_for_render(tl)) == 1


def test_voice_combines_with_hard_mute():
    # Hard mute still becomes a volume filter on the (voice-removed) audio input.
    cmd, _, n_mute = build_command(
        Path("in.mkv"),
        _tl(_seg(1, 1000, 2000, "voice"), _seg(2, 5000, 6000, "mute")),
        Path("out.mkv"),
        audio_path=Path("voiced.pcm"), audio_sr=44100, audio_ch=2,
    )
    assert n_mute == 1
    assert "-af" in cmd
    assert "volume=enable='between(t,5.000,6.000)':volume=0" in cmd


def test_voice_and_skip_reference_the_new_audio_input():
    cmd, _, _ = build_command(
        Path("in.mkv"),
        _tl(_seg(1, 1000, 2000, "voice"), _seg(2, 10_000, 12_000, "skip")),
        Path("out.mkv"),
        duration_s=60.0,
        audio_path=Path("voiced.pcm"), audio_sr=44100, audio_ch=2,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    # The cut splits the voice-removed audio (input 1), not the source.
    assert "[1:a]asplit" in fc
    assert "[0:a]asplit" not in fc
