import json

from worker.models import Segment, Timeline
from worker.review import (
    clip_path,
    load_timeline,
    render_page,
    set_approval,
    set_approvals,
    sidecar_for,
)


def _film(tmp_path):
    media = tmp_path / "Film.mkv"
    media.touch()
    timeline = Timeline(
        mediaFingerprint="fp",
        segments=[
            Segment(id=1, startMs=1000, endMs=2000, category="nudity",
                    confidence=0.9, engine="vlm", recommendedAction="skip"),
            Segment(id=2, startMs=5000, endMs=5800, category="profanity",
                    confidence=1.0, engine="subtitles", recommendedAction="mute"),
        ],
    )
    sidecar_for(media).write_text(json.dumps(timeline.model_dump()), encoding="utf-8")
    return media, timeline


def test_approval_persists_to_sidecar(tmp_path):
    media, _ = _film(tmp_path)

    assert set_approval(media, 1, True)
    assert load_timeline(media).segments[0].approved is True

    # rejecting is distinct from undecided — it must survive a reload
    assert set_approval(media, 2, False)
    reloaded = load_timeline(media)
    assert reloaded.segments[1].approved is False
    assert reloaded.segments[0].approved is True


def test_approval_can_be_cleared(tmp_path):
    media, _ = _film(tmp_path)
    set_approval(media, 1, True)
    set_approval(media, 1, None)
    assert load_timeline(media).segments[0].approved is None


def test_unknown_segment_is_rejected(tmp_path):
    media, _ = _film(tmp_path)
    assert not set_approval(media, 99, True)


def test_bulk_approval_sets_many_in_one_write(tmp_path):
    media, _ = _film(tmp_path)

    changed = set_approvals(media, [1, 2], True)

    assert changed == 2
    assert all(s.approved is True for s in load_timeline(media).segments)


def test_bulk_approval_ignores_unknown_ids(tmp_path):
    """A finding deleted in another tab must not fail the whole action."""
    media, _ = _film(tmp_path)

    changed = set_approvals(media, [1, 99], False)

    assert changed == 1
    reloaded = load_timeline(media)
    assert reloaded.segments[0].approved is False
    assert reloaded.segments[1].approved is None


def test_bulk_approval_can_clear_decisions(tmp_path):
    media, _ = _film(tmp_path)
    set_approvals(media, [1, 2], True)

    set_approvals(media, [1, 2], None)

    assert all(s.approved is None for s in load_timeline(media).segments)


def test_missing_analysis_returns_none(tmp_path):
    media = tmp_path / "Nothing.mkv"
    media.touch()
    assert load_timeline(media) is None


def test_page_embeds_segments_and_escapes_path(tmp_path):
    media, timeline = _film(tmp_path)
    html = render_page(media, timeline)
    assert "nudity" in html and "profanity" in html
    # Windows paths are backslash-heavy; they must survive into JS intact
    assert json.dumps(str(media))[1:-1].split("\\\\")[-1] in html.replace("\\\\", "\\\\")
    assert "2 finding(s)" in html


def test_page_has_review_controls(tmp_path):
    """The muted-clip preview, filter toggle and timing badge are present."""
    media, timeline = _film(tmp_path)
    html = render_page(media, timeline)
    assert "Play muted" in html  # verify a profanity mute lands on the word
    assert "Play flagged part only" in html
    assert "Undecided" in html  # focus filter
    assert "exact timing" in html or "timing" in html


def test_page_has_type_filter_and_bulk_controls(tmp_path):
    """Reviewers can group by type and settle a whole group at once."""
    media, timeline = _film(tmp_path)
    html = render_page(media, timeline)
    assert "typeFilters" in html  # per-type filter row
    assert "All types" in html
    assert "buildTypeFilters" in html
    # bulk "apply to all shown" actions, wired to the batch endpoint
    assert 'id=bulk' in html
    assert "shown:" in html
    assert "function bulkSet" in html
    assert "method: 'PATCH'" in html


def test_muted_clip_has_its_own_cache_path(tmp_path):
    """Muted and plain clips of the same span must not collide in the cache."""
    media = tmp_path / "Film.mkv"
    plain = clip_path(media, 1000, 2000, 15.0, mute=False)
    muted = clip_path(media, 1000, 2000, 15.0, mute=True)
    assert plain != muted


def test_build_peaks_window_and_localization(tmp_path):
    """The waveform spans the finding ±pad, at 40 peaks/s, and the loud burst
    lands at the finding — so a reviewer can see the word to drag onto it."""
    import wave

    import numpy as np

    from worker.review import build_peaks

    sr = 8000
    wav = tmp_path / "tone.wav"
    buf = np.zeros(sr * 4, dtype=np.int16)
    buf[sr : sr + int(0.3 * sr)] = (15000 * np.sin(
        2 * np.pi * 900 * np.arange(int(0.3 * sr)) / sr)).astype(np.int16)  # burst at 1.0s
    with wave.open(str(wav), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(buf.tobytes())

    r = build_peaks(wav, 1000, 1300, pad_s=0.5)
    assert (r["winStartMs"], r["winEndMs"], r["perSec"]) == (500, 1800, 40)
    assert abs(len(r["peaks"]) - round(1.3 * 40)) <= 1  # 40 peaks/s over the window
    loud = max(range(len(r["peaks"])), key=lambda i: r["peaks"][i])
    loud_ms = r["winStartMs"] + loud * (1000 // r["perSec"])
    assert 900 <= loud_ms <= 1350  # burst localizes at the finding (1000–1300ms)


def test_build_filmstrip_returns_jpeg(tmp_path):
    """The visual timing editor's frame strip is one tiled JPEG for the window."""
    import subprocess

    from worker.review import build_filmstrip

    media = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=10:duration=12",
         "-c:v", "libx264", str(media)],
        check=True,
    )
    jpeg = build_filmstrip(media, 5000, 7000, pad_s=2.0)
    assert jpeg is not None and jpeg[:2] == b"\xff\xd8"  # JPEG magic
