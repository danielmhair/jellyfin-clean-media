"""Profanity word matching and segment merging for the Whisper adapter.

The default list targets strong profanity and slurs an administrator would
typically mute for family viewing. It is intentionally conservative about
mild words ("hell", "damn") — those are opt-in via the job's options:
{"extraWords": [...], "includeMild": true}.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..settings import get_settings

# Matched as exact normalized words.
STRONG_WORDS = {
    "fuck", "fucker", "fucking", "fucked", "motherfucker", "motherfucking",
    "shit", "shitty", "bullshit", "horseshit", "shithead",
    "asshole", "assholes",
    "bitch", "bitches", "bitching",
    "bastard", "bastards",
    "cunt", "cunts",
    "cock", "cocks", "dick", "dicks", "dickhead",
    "prick", "pricks",
    "pussy", "pussies",
    "whore", "whores", "slut", "sluts",
    "ass", "asses",
    "goddamn", "goddamnit", "goddammit",
    "hell", "damn", "damnit", "dammit",
    "nigger", "niggers", "nigga",
    "faggot", "faggots", "fag",
    "retard", "retarded",
    "jackass", "dumbass", "badass",
    "piss", "pissed", "pissing",
}

# Word families caught by prefix (handles fuckin', shittin', etc.)
STRONG_PREFIXES = ("fuck", "motherfuck", "bullshit", "goddam")

# Opt-in via includeMild. Not swearing — the kid-level register of insults
# and toilet words that parents of young children often want gone even
# though no adult would call them profanity. Real swear words, including
# hell and damn, live in STRONG_WORDS and need no opt-in.
MILD_WORDS = {
    "stupid", "idiot", "idiots", "idiotic", "dumb", "dummy",
    "moron", "morons", "moronic", "jerk", "jerks", "loser", "losers",
    "poop", "poopy", "fart", "farted", "farting", "butt", "butts",
    "shutup", "freak", "freaks", "sucks", "hate",
}

# Taking the Lord's name in vain. Separate from mild so it can be enabled on
# its own. "goddamn" is already in STRONG_WORDS.
BLASPHEMY_WORDS = {
    "god", "gods", "jesus", "christ", "jeez", "geez", "jesu",
}

_NORM_RE = re.compile(r"[^a-z']+")


def normalize(word: str) -> str:
    w = _NORM_RE.sub("", word.lower())
    return w.strip("'")


def is_profane(
    word: str,
    include_mild: bool = False,
    extra: set[str] | None = None,
    include_blasphemy: bool = False,
) -> bool:
    w = normalize(word)
    if not w:
        return False
    if w in STRONG_WORDS or w.startswith(STRONG_PREFIXES):
        return True
    if include_mild and w in MILD_WORDS:
        return True
    if include_blasphemy and w in BLASPHEMY_WORDS:
        return True
    return bool(extra and w in extra)


def resolve_flags(options: dict) -> tuple[bool, bool, set[str]]:
    """Merge per-job profanity options with the worker's saved defaults.

    A per-job option always wins (existing CLI/direct-API callers keep
    working unchanged); the settings store (the plugin's Advanced tab) is
    the fallback default, so a plugin save changes untouched jobs without
    breaking a caller that already sets its own flags explicitly.
    ``extraWords`` is additive from both sources — a one-off job extra word
    shouldn't require repeating everything already configured on the plugin.
    """
    stored = get_settings()
    include_mild = bool(options.get("includeMild", stored.profanityIncludeMild))
    include_blasphemy = bool(options.get("includeBlasphemy", stored.profanityIncludeBlasphemy))
    extra = {w.lower() for w in options.get("extraWords", [])}
    extra |= {w.strip().lower() for w in stored.profanityExtraWords.split(",") if w.strip()}
    return include_mild, include_blasphemy, extra


@dataclass
class Hit:
    startMs: int
    endMs: int
    word: str
    confidence: float
    context: str


def merge_hits(hits: list[Hit], gap_ms: int = 750) -> list[Hit]:
    """Merge hits whose padded windows are close, so one mute covers a burst."""
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h.startMs)
    merged = [hits[0]]
    for h in hits[1:]:
        prev = merged[-1]
        if h.startMs - prev.endMs <= gap_ms:
            prev.endMs = max(prev.endMs, h.endMs)
            prev.word = f"{prev.word}, {h.word}"
            prev.confidence = min(prev.confidence, h.confidence)
            if h.context and h.context not in prev.context:
                prev.context = f"{prev.context} … {h.context}"
        else:
            merged.append(h)
    return merged
