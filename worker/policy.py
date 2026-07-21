"""Turn what the model saw into what the administrator considers a finding.

The VLM reports observations — "a man is bare-chested", "a woman's chest is
visible" — and this module decides which of those matter. Keeping policy
out of the prompt matters for two reasons:

* Models do not reliably obey negative instructions. Told "a shirtless man
  is NOT nudity", one flagged 50 shots of a shirtless character as nudity
  anyway, including one it described as "no explicit nudity".
* Policy changes are free. Observations are stored per shot, so changing
  what counts re-derives findings instantly rather than costing another
  multi-hour pass over the film.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Observation keys the model is asked to answer true/false.
OBSERVATIONS = (
    "female_topless",
    "buttocks_or_genitals",
    "underwear_only",
    "male_shirtless",
    "sex_act",
    "kissing",
    "kissing_sexual",
    "sexualised_framing",
)


@dataclass
class Policy:
    """What an administrator wants flagged, and how firmly."""

    # A bare male chest on its own is ordinary in film — sport, swimming,
    # fighting. It only becomes a question when the camera dwells on it,
    # and then it is a question, not a verdict.
    flag_male_shirtless: bool = False
    flag_underwear: bool = True
    flag_any_kissing: bool = False
    # Categories reported for review but never treated as certain.
    tentative: frozenset[str] = field(default_factory=lambda: frozenset({"suggestive"}))


def classify(obs: dict, policy: Policy | None = None) -> str | None:
    """Return a category for these observations, or None if nothing applies.

    Ordered most to least serious, so a frame that is several things at
    once is reported as the most significant.
    """
    policy = policy or Policy()

    if obs.get("sex_act"):
        return "sexual_activity"
    if obs.get("female_topless") or obs.get("buttocks_or_genitals"):
        return "nudity"
    if obs.get("kissing_sexual"):
        return "intense_kissing"
    if policy.flag_underwear and obs.get("underwear_only"):
        return "suggestive"
    # A shirtless man counts only when the framing sexualises him — unless
    # the administrator asks for every instance.
    if obs.get("male_shirtless"):
        if policy.flag_male_shirtless:
            return "suggestive"
        if obs.get("sexualised_framing"):
            return "suggestive"
        return None
    if obs.get("sexualised_framing"):
        return "suggestive"
    if policy.flag_any_kissing and obs.get("kissing"):
        return "intense_kissing"
    return None


def is_tentative(category: str, policy: Policy | None = None) -> bool:
    return category in (policy or Policy()).tentative
