import json

from worker.models import Segment, Timeline
from worker.review import load_timeline, render_page, set_approval, sidecar_for


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
