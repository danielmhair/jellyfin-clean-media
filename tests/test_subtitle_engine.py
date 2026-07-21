from worker.engines.subtitle_engine import SubtitleEngine
from worker.engines.subtitles import parse_srt

SRT = """1
00:00:10,000 --> 00:00:12,000
<i>(EXPLOSION)</i> Son of a bitch!

2
00:00:20,000 --> 00:00:22,500
TONY: What the hell was that?

3
00:00:30,000 --> 00:00:32,000
You stupid person.

4
00:00:40,000 --> 00:00:42,000
Nothing objectionable here.
"""


def _write(tmp_path):
    p = tmp_path / "movie.en.srt"
    p.write_text(SRT, encoding="utf-8")
    return p


def test_parse_strips_tags_and_sound_cues(tmp_path):
    cues = parse_srt(_write(tmp_path))
    assert len(cues) == 4
    assert cues[0].text == "Son of a bitch!"
    assert cues[0].startMs == 10_000
    assert cues[1].text == "What the hell was that?"  # speaker label removed


def test_swearing_is_flagged_without_opt_in(tmp_path):
    """bitch and hell are both swearing; neither needs a flag set."""
    media = tmp_path / "movie.mkv"
    media.touch()
    _write(tmp_path)

    timeline, srt = SubtitleEngine().analyze(
        media, "fp", {"preciseTiming": False}, lambda f, s: None
    )

    assert len(timeline.segments) == 2
    seg = timeline.segments[0]
    assert seg.category == "profanity"
    assert seg.recommendedAction == "mute"
    assert "bitch" in seg.reasoning
    # window stays inside the cue
    assert seg.startMs >= 10_000 - 200
    assert seg.endMs <= 12_000 + 200
    assert "hell" in timeline.segments[1].reasoning


def test_include_mild_adds_kid_words(tmp_path):
    media = tmp_path / "movie.mkv"
    media.touch()
    _write(tmp_path)

    timeline, _ = SubtitleEngine().analyze(
        media, "fp", {"includeMild": True, "preciseTiming": False}, lambda f, s: None
    )
    assert len(timeline.segments) == 3
    assert "stupid" in timeline.segments[2].reasoning


def test_blasphemy_opt_in(tmp_path):
    media = tmp_path / "movie.mkv"
    media.touch()
    p = tmp_path / "movie.en.srt"
    p.write_text(
        "1\n00:00:05,000 --> 00:00:07,000\nOh, my God, are you okay?\n", encoding="utf-8"
    )

    off, _ = SubtitleEngine().analyze(
        media, "fp", {"preciseTiming": False}, lambda f, s: None
    )
    assert off.segments == []

    on, _ = SubtitleEngine().analyze(
        media, "fp", {"includeBlasphemy": True, "preciseTiming": False}, lambda f, s: None
    )
    assert len(on.segments) == 1
    assert "god" in on.segments[0].reasoning.lower()


def test_estimated_window_stays_inside_cue(tmp_path):
    """A word late in a long cue must not mute the words before it."""
    media = tmp_path / "movie.mkv"
    media.touch()
    p = tmp_path / "movie.en.srt"
    p.write_text(
        "1\n00:00:10,000 --> 00:00:20,000\n"
        "This is a very long line of dialogue ending in bitch\n",
        encoding="utf-8",
    )

    timeline, _ = SubtitleEngine().analyze(
        media, "fp", {"preciseTiming": False}, lambda f, s: None
    )
    seg = timeline.segments[0]
    # the word is at the end, so the window must be in the cue's second half
    assert seg.startMs > 15_000
    assert seg.endMs <= 20_000 + 1500 + 200


def test_whole_cue_option(tmp_path):
    media = tmp_path / "movie.mkv"
    media.touch()
    _write(tmp_path)

    timeline, _ = SubtitleEngine().analyze(
        media, "fp", {"wholeCue": True}, lambda f, s: None
    )
    seg = timeline.segments[0]
    assert seg.startMs == 10_000 - 200
    assert seg.endMs == 12_000 + 200
