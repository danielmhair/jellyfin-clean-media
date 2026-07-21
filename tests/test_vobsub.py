from pathlib import Path

from worker.engines.subtitles import Cue, parse_srt, repair_mojibake, write_srt
from worker.engines.vobsub import _clean

# Written as escapes, not literals, so no editor or tool can silently
# normalise the very bytes under test.
CURLY = "beyond God’s boundaries"
MOJIBAKE = "beyond Godâ€™s boundaries"


def test_ocr_cleanup_fixes_common_confusions():
    assert _clean("|'m not going") == "I'm not going"
    # zero misread for the letter o, but only between letters
    assert _clean("s0me  text\nhere") == "some text here"
    assert _clean("- Get out!") == "Get out!"


def test_forced_track_is_not_selected(monkeypatch):
    """Discs carry several English tracks; only one holds the dialogue."""
    from worker.engines import vobsub

    monkeypatch.setattr(
        vobsub,
        "subtitle_packet_counts",
        lambda _p: {
            5: {"codec": "dvd_subtitle", "packets": 12, "language": "eng"},
            6: {"codec": "dvd_subtitle", "packets": 1333, "language": "eng"},
            7: {"codec": "dvd_subtitle", "packets": 996, "language": "spa"},
            10: {"codec": "dvd_subtitle", "packets": 12, "language": "eng"},
        },
    )
    assert vobsub.find_image_subtitle_stream(Path("x.mkv"), "eng") == 6


def test_signage_only_disc_returns_none(monkeypatch):
    from worker.engines import vobsub

    monkeypatch.setattr(
        vobsub,
        "subtitle_packet_counts",
        lambda _p: {5: {"codec": "dvd_subtitle", "packets": 12, "language": "eng"}},
    )
    assert vobsub.find_image_subtitle_stream(Path("x.mkv"), "eng") is None


def test_mojibake_from_old_ocr_runs_is_repaired():
    """UTF-8 bytes decoded as cp1252, as written by OCR runs before the fix."""
    assert repair_mojibake(MOJIBAKE) == CURLY
    # already-correct text must survive untouched
    assert repair_mojibake(CURLY) == CURLY
    assert repair_mojibake("naïve café") == "naïve café"


def test_smart_quotes_normalised():
    """Curly punctuation must not reach the word matcher as-is."""
    assert _clean("That’s a hell of a business.") == "That's a hell of a business."
    assert _clean("“Get out”") == '"Get out"'


def test_digits_are_not_mangled():
    """Years and counts must survive: only letter-flanked zeros are fixed."""
    assert _clean("in 2003 he lost 10 men") == "in 2003 he lost 10 men"


def test_srt_roundtrip(tmp_path):
    """OCR output is cached as SRT, so it must survive a write/read cycle."""
    cues = [Cue(1_000, 3_500, "Son of a bitch!"), Cue(4_000, 5_250, "What the hell?")]
    path = write_srt(cues, tmp_path / "out.srt")

    reparsed = parse_srt(path)
    assert len(reparsed) == 2
    assert reparsed[0].startMs == 1_000
    assert reparsed[0].endMs == 3_500
    assert reparsed[0].text == "Son of a bitch!"
    assert reparsed[1].text == "What the hell?"


def test_parsing_repairs_a_mojibake_srt(tmp_path):
    path = tmp_path / "broken.srt"
    path.write_text(
        f"1\n00:00:01,000 --> 00:00:03,000\n{MOJIBAKE}\n", encoding="utf-8"
    )
    assert parse_srt(path)[0].text == CURLY


def test_srt_timestamp_format(tmp_path):
    path = write_srt([Cue(3_661_042, 3_662_000, "x")], tmp_path / "t.srt")
    assert "01:01:01,042 --> 01:01:02,000" in path.read_text(encoding="utf-8")
