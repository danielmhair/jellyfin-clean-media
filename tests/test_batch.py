from pathlib import Path

from worker.batch import discover


def test_discover_expands_folders_and_globs(tmp_path):
    (tmp_path / "a.mkv").touch()
    (tmp_path / "b.mp4").touch()
    (tmp_path / "notes.txt").touch()
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.mkv").touch()

    names = {p.name for p in discover([str(tmp_path)])}
    assert names == {"a.mkv", "b.mp4", "c.mkv"}


def test_rendered_copies_are_not_reanalyzed(tmp_path):
    """A clean copy is an output; feeding it back in would compound edits."""
    (tmp_path / "Film.mkv").touch()
    (tmp_path / "Film (Clean).mkv").touch()

    names = {p.name for p in discover([str(tmp_path)])}
    assert names == {"Film.mkv"}


def test_duplicates_are_collapsed(tmp_path):
    (tmp_path / "a.mkv").touch()
    found = discover([str(tmp_path), str(tmp_path / "a.mkv")])
    assert len(found) == 1


def test_missing_paths_are_ignored(tmp_path):
    assert discover([str(tmp_path / "nope-*.mkv")]) == []
