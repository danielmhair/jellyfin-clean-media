"""Migrating legacy cleaned/ copies to the '<movie> - Clean' version path.

Moving a user's media files is destructive, so the plan is pinned here: which
files move, which are left alone, and that an emptied cleaned/ folder is cleared.
"""

from __future__ import annotations

import pytest

import worker.migrate_clean_copies as mig
from worker.migrate_clean_copies import main, plan, resolve_roots


def _film(root, folder, stem, ext=".mkv"):
    """A film in `folder` with a legacy clean copy in its cleaned/ subfolder."""
    d = root / folder
    (d / "cleaned").mkdir(parents=True)
    (d / f"{stem}{ext}").write_bytes(b"original")
    (d / "cleaned" / f"{stem} (Clean){ext}").write_bytes(b"clean copy")
    return d


def test_plan_targets_the_version_path_for_a_film_in_its_own_folder(tmp_path):
    _film(tmp_path, "Iron Man (2008)", "Iron Man (2008)")

    results = list(plan([tmp_path]))

    assert len(results) == 1
    src, target = results[0]
    assert src == tmp_path / "Iron Man (2008)" / "cleaned" / "Iron Man (2008) (Clean).mkv"
    assert target == tmp_path / "Iron Man (2008)" / "Iron Man (2008) - Clean.mkv"


def test_plan_skips_a_flat_library_that_cannot_be_a_version(tmp_path):
    # Parent folder name ("Movies") != the film stem, so version grouping can't
    # work — reported with target None rather than mis-named.
    _film(tmp_path, "Movies", "Some Film (2010)")

    results = list(plan([tmp_path]))

    assert results == [
        (tmp_path / "Movies" / "cleaned" / "Some Film (2010) (Clean).mkv", None)
    ]


def test_apply_moves_the_file_and_removes_the_emptied_cleaned_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANMEDIA_DB", str(tmp_path / "jobs.db"))  # not the real store
    folder = _film(tmp_path, "Iron Man (2008)", "Iron Man (2008)")

    rc = main(["--apply", str(tmp_path)])

    assert rc == 0
    assert (folder / "Iron Man (2008) - Clean.mkv").read_bytes() == b"clean copy"
    assert not (folder / "cleaned").exists()  # emptied folder tidied away


def test_resolve_roots_prefers_explicit_then_env_then_worker(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    envdir = tmp_path / "env"
    workerdir = tmp_path / "worker"
    for d in (explicit, envdir, workerdir):
        d.mkdir()

    # Explicit args win outright.
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(envdir))
    monkeypatch.setattr(mig, "_roots_from_worker", lambda: [workerdir])
    assert resolve_roots([str(explicit)]) == [explicit]

    # No args but env set -> env.
    assert resolve_roots([]) == [envdir]

    # No args, no env -> ask the worker.
    monkeypatch.delenv("CLEANMEDIA_MEDIA_ROOTS", raising=False)
    assert resolve_roots([]) == [workerdir]


def test_dry_run_moves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANMEDIA_DB", str(tmp_path / "jobs.db"))
    folder = _film(tmp_path, "Iron Man (2008)", "Iron Man (2008)")

    main([str(tmp_path)])  # no --apply

    assert (folder / "cleaned" / "Iron Man (2008) (Clean).mkv").exists()
    assert not (folder / "Iron Man (2008) - Clean.mkv").exists()
