from worker.policy import Policy, classify

NOTHING = {k: False for k in (
    "female_topless", "buttocks_or_genitals", "underwear_only",
    "male_shirtless", "sex_act", "kissing", "kissing_sexual",
    "sexualised_framing",
)}


def obs(**kw):
    return {**NOTHING, **kw}


def test_empty_frame_is_not_flagged():
    assert classify(obs()) is None


def test_shirtless_man_alone_is_not_nudity():
    """The regression that motivated this module: 50 shots of a shirtless
    character reported as nudity, one described as 'no explicit nudity'."""
    assert classify(obs(male_shirtless=True)) is None


def test_shirtless_man_sexualised_is_a_question_not_a_verdict():
    assert classify(obs(male_shirtless=True, sexualised_framing=True)) == "suggestive"


def test_shirtless_man_can_be_flagged_by_policy():
    assert classify(
        obs(male_shirtless=True), Policy(flag_male_shirtless=True)
    ) == "suggestive"


def test_topless_woman_is_nudity():
    assert classify(obs(female_topless=True)) == "nudity"


def test_woman_turned_away_still_counts():
    """A bare back showing she is undressed is nudity even with nothing explicit."""
    assert classify(obs(female_topless=True, male_shirtless=False)) == "nudity"


def test_exposed_private_parts_are_nudity_for_anyone():
    assert classify(obs(buttocks_or_genitals=True)) == "nudity"


def test_sex_act_outranks_everything():
    assert classify(obs(sex_act=True, male_shirtless=True, kissing=True)) == "sexual_activity"


def test_ordinary_kissing_is_not_flagged():
    assert classify(obs(kissing=True)) is None


def test_private_kissing_is_flagged():
    assert classify(obs(kissing=True, kissing_sexual=True)) == "intense_kissing"


def test_ordinary_kissing_flagged_when_policy_asks():
    assert classify(obs(kissing=True), Policy(flag_any_kissing=True)) == "intense_kissing"


def test_underwear_is_suggestive_and_can_be_disabled():
    assert classify(obs(underwear_only=True)) == "suggestive"
    assert classify(obs(underwear_only=True), Policy(flag_underwear=False)) is None


def test_nudity_outranks_suggestive():
    assert classify(obs(female_topless=True, sexualised_framing=True)) == "nudity"
