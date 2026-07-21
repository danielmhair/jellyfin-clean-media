from worker.engines.profanity import Hit, is_profane, merge_hits


def test_strong_words_matched():
    assert is_profane("Fucking")
    assert is_profane("fuckin'")  # prefix family catches dropped g
    assert is_profane("SHIT")
    assert is_profane("bullshit,")


def test_clean_words_pass():
    for w in ["ship", "class", "hello", "assistant", "scrap", "molasses", "bass"]:
        assert not is_profane(w), w


def test_administrator_swear_words_need_no_opt_in():
    """These are real swear words, not borderline exclamations."""
    for w in ["dick", "prick", "pricks", "whore", "bitch", "jackass", "ass", "asses"]:
        assert is_profane(w), w


def test_hell_and_damn_are_swearing_not_mild():
    """These need no opt-in; an administrator called them real profanity."""
    for w in ["hell", "damn", "damnit", "dammit"]:
        assert is_profane(w), w


def test_mild_is_kid_level_name_calling():
    for w in ["stupid", "idiot", "poop", "jerk", "butt"]:
        assert not is_profane(w), w
        assert is_profane(w, include_mild=True), w


def test_extra_words():
    assert not is_profane("frak")
    assert is_profane("frak", extra={"frak"})


def test_merge_nearby_hits():
    hits = [
        Hit(startMs=1000, endMs=1500, word="a", confidence=0.9, context="x"),
        Hit(startMs=1900, endMs=2400, word="b", confidence=0.8, context="y"),
        Hit(startMs=9000, endMs=9500, word="c", confidence=0.7, context="z"),
    ]
    merged = merge_hits(hits, gap_ms=750)
    assert len(merged) == 2
    assert merged[0].startMs == 1000
    assert merged[0].endMs == 2400
    assert merged[0].confidence == 0.8
    assert merged[1].word == "c"
