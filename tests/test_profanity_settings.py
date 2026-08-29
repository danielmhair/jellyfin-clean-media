"""worker/engines/profanity.py's resolve_flags: merging per-job options with
the worker's saved defaults (worker/settings.py, editable from the plugin's
Advanced tab)."""

from __future__ import annotations

from worker import settings as worker_settings
from worker.engines.profanity import resolve_flags
from worker.settings import WorkerSettings


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_settings, "_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(worker_settings, "_current", None)


def test_defaults_when_nothing_configured(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    include_mild, include_blasphemy, extra = resolve_flags({})
    assert include_mild is False
    assert include_blasphemy is False
    assert extra == set()


def test_settings_store_is_the_fallback_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    worker_settings.set_settings(
        WorkerSettings(
            profanityIncludeMild=True,
            profanityIncludeBlasphemy=True,
            profanityExtraWords="heck, darn ,, ",
        )
    )
    include_mild, include_blasphemy, extra = resolve_flags({})
    assert include_mild is True
    assert include_blasphemy is True
    assert extra == {"heck", "darn"}


def test_per_job_option_overrides_the_stored_flag(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    worker_settings.set_settings(WorkerSettings(profanityIncludeMild=True))
    # An explicit False from a caller wins over the stored True.
    include_mild, _, _ = resolve_flags({"includeMild": False})
    assert include_mild is False


def test_extra_words_are_additive_not_overriding(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    worker_settings.set_settings(WorkerSettings(profanityExtraWords="fromplugin"))
    _, _, extra = resolve_flags({"extraWords": ["fromjob"]})
    assert extra == {"fromplugin", "fromjob"}
