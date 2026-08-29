import os

from worker import settings as worker_settings
from worker.review import resolve_media
from worker.settings import WorkerSettings


def test_exact_path_wins(tmp_path, monkeypatch):
    film = tmp_path / "Film.mkv"
    film.touch()
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    assert resolve_media(str(film)) == film


def test_jellyfin_nas_path_maps_to_local_file(tmp_path, monkeypatch):
    """Jellyfin sends its own mount; the worker has the same film elsewhere."""
    local = tmp_path / "Movies" / "Some Film (2010).mkv"
    local.parent.mkdir()
    local.touch()
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))

    assert resolve_media("/volume1/Media/Movies/Some Film (2010).mkv") == local


def test_windows_path_from_another_host(tmp_path, monkeypatch):
    local = tmp_path / "Film.mkv"
    local.touch()
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    assert resolve_media(r"D:\Media\Movies\Film.mkv") == local


def test_unknown_film_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    assert resolve_media("/volume1/Movies/Not Here.mkv") is None


def test_multiple_roots_are_searched(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    film = b / "Film.mkv"
    film.touch()
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", os.pathsep.join([str(a), str(b)]))
    assert resolve_media("/elsewhere/Film.mkv") == film


def test_settings_store_media_roots_wins_over_env(tmp_path, monkeypatch):
    """A media-roots change made only via the plugin (never touching the env
    var) must still resolve — and, crucially, must invalidate the cached
    media index (see worker/review.py's _ensure_index staleness signature),
    or the review grid would keep serving the old roots' files for up to the
    30-minute cache TTL after a "live, no restart needed" plugin save."""
    monkeypatch.setattr(worker_settings, "_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(worker_settings, "_current", None)

    from_env = tmp_path / "from-env"
    from_env.mkdir()
    (from_env / "Env Film.mkv").touch()
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(from_env))

    from_plugin = tmp_path / "from-plugin"
    from_plugin.mkdir()
    plugin_film = from_plugin / "Plugin Film.mkv"
    plugin_film.touch()
    worker_settings.set_settings(WorkerSettings(mediaRoots=str(from_plugin)))

    # The settings-store value wins outright, and its own index is built —
    # not a stale cache left over from the env-only roots.
    assert resolve_media("/elsewhere/Plugin Film.mkv") == plugin_film
    assert resolve_media("/elsewhere/Env Film.mkv") is None
