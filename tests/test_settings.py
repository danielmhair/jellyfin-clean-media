"""Worker-owned settings persistence (worker/settings.py).

Mirrors worker/schedule.py's pattern (a pydantic model in a JSON file under
DATA_DIR) — these tests check the same properties: defaults, disk round-trip,
and tolerance of a corrupt file, plus the one thing unique to this module:
that its defaults line up with what Policy() and the VLM host fallback chain
already assume.
"""

from __future__ import annotations

import pytest

from worker import settings as worker_settings
from worker.policy import Policy
from worker.settings import WorkerSettings


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Every test gets its own settings file and a cleared in-memory cache,
    so a value saved in one test can't leak into the next via the shared
    module-level cache (the same hazard worker/update.py's tests navigate)."""
    monkeypatch.setattr(worker_settings, "_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(worker_settings, "_current", None)


def test_defaults_match_policy_dataclass():
    s = WorkerSettings()
    assert s.mediaRoots == ""
    assert s.vlmHosts == ""
    assert s.vlmGuidance == {}
    policy = Policy()
    assert s.flagMaleShirtless == policy.flag_male_shirtless
    assert s.flagUnderwear == policy.flag_underwear
    assert s.flagAnyKissing == policy.flag_any_kissing
    assert s.profanityIncludeMild is False
    assert s.profanityIncludeBlasphemy is False
    assert s.profanityExtraWords == ""
    assert s.supervisorEnabled is True


def test_get_settings_returns_defaults_when_nothing_saved():
    s = worker_settings.get_settings()
    assert s == WorkerSettings()


def test_set_then_get_round_trips_through_disk():
    saved = worker_settings.set_settings(
        WorkerSettings(mediaRoots="/movies", vlmHosts="http://x:11434", flagAnyKissing=True)
    )
    assert saved.mediaRoots == "/movies"

    # Force a fresh read from disk, not the in-memory cache.
    worker_settings._current = None
    reloaded = worker_settings.get_settings()
    assert reloaded.mediaRoots == "/movies"
    assert reloaded.vlmHosts == "http://x:11434"
    assert reloaded.flagAnyKissing is True


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    worker_settings._PATH.parent.mkdir(parents=True, exist_ok=True)
    worker_settings._PATH.write_text("not valid json {{{", encoding="utf-8")
    assert worker_settings.get_settings() == WorkerSettings()


def test_view_includes_real_default_guidance_text():
    view = worker_settings.view()
    assert view.settings == WorkerSettings()
    # Every observation key has real, non-empty default guidance text — the
    # UI shows this as placeholder/reset content, so an empty entry here
    # would silently break the Advanced tab's "Reset to default" button.
    from worker.policy import OBSERVATIONS

    for key in OBSERVATIONS:
        assert view.vlmGuidanceDefault.get(key)
