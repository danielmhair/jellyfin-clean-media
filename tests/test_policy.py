"""worker/policy.py's fixed JSON-schema footer.

Pinned byte-for-byte against the prompt's original hardcoded text: the VLM
guidance became partly admin-editable (worker/settings.py's vlmGuidance), but
the JSON contract worker.policy.classify() parses must never move — this
test is what would catch an accidental drift (e.g. someone reordering
OBSERVATIONS), which would otherwise just look like "checkpoints keep
getting discarded" via vlm_engine._prompt_digest(), not an obvious bug.
"""

from __future__ import annotations

import json

from worker.policy import OBSERVATIONS, observe_json_footer


def test_footer_matches_original_hardcoded_text():
    expected = (
        'Respond with JSON only:\n'
        '{"female_topless": false, "buttocks_or_genitals": false, "underwear_only": false, '
        '"male_shirtless": false, "sex_act": false, "kissing": false, "kissing_sexual": false, '
        '"sexualised_framing": false, "description": "<what you see, under 12 words>"}'
    )
    assert observe_json_footer() == expected


def test_footer_contains_every_observation_key_in_order():
    footer = observe_json_footer()
    schema_line = footer.split("\n", 1)[1]
    parsed = json.loads(schema_line)
    assert list(parsed.keys()) == [*OBSERVATIONS, "description"]
    assert all(parsed[k] is False for k in OBSERVATIONS)
