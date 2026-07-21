import os

from worker.review import resolve_media


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
