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

import json
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

# The default per-field definition text for each observation — what an
# administrator can override from the plugin's Advanced tab. This is the only
# part of the VLM prompt that is ever user-editable: the calibration
# paragraphs above it ("ignore statues/mannequins", "clothing counts as
# clothing") and the JSON-schema footer below it are fixed in code, so no
# edit here can ever change *what* the model is asked to return — only how it
# judges each field, which is the actual "what counts as nudity/suggestive"
# knob a family would want to turn.
DEFAULT_FIELD_GUIDANCE: dict[str, str] = {
    "female_topless": (
        "a woman's BARE breast or nipple is visible, OR she is clearly nude "
        "seen from behind with bare back and buttocks. A clothed back, "
        "straps, or a bare shoulder alone is false."
    ),
    "buttocks_or_genitals": (
        "actual BARE skin of buttocks or genitals is visible on a real "
        "person. Anyone clothed — trousers, shorts, a jumpsuit, tight "
        "outfit, underwear — is false."
    ),
    "underwear_only": "a person is in bra/underwear/lingerie with nothing over it.",
    "male_shirtless": "a man's bare chest is visible.",
    "sex_act": (
        "people are actively having sex or simulating it, or lying together "
        "in evident intimate physical contact in bed."
    ),
    "kissing": (
        "two people's LIPS ARE TOUCHING. Faces merely close, foreheads "
        "together, an embrace, or about-to-kiss is false."
    ),
    "kissing_sexual": (
        "lips are touching AND it is sustained open-mouthed kissing with "
        "roaming hands or partial undress."
    ),
    "sexualised_framing": (
        "the camera lingers on a real body as an object of desire — posing, "
        "stripping — not incidental (sport, fighting, washing, medical)."
    ),
}


def observe_json_footer() -> str:
    """The fixed JSON-schema tail of the VLM prompt, generated from OBSERVATIONS.

    Never editable from the plugin — classify() depends on this exact key
    set, so no admin edit to a field's guidance text can ever break the
    parse; at worst it makes a field's judgement worse, never crashes the
    pass.
    """
    fields = {name: False for name in OBSERVATIONS}
    fields["description"] = "<what you see, under 12 words>"
    return "Respond with JSON only:\n" + json.dumps(fields)


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
