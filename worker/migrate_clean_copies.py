"""Move clean copies from the legacy ``cleaned/`` subfolder to the version path.

Older renders wrote a film's clean copy to
``<movie folder>/cleaned/<stem> (Clean).<ext>``. Jellyfin only groups it as a
selectable *version* of the movie when it sits beside the original as
``<folder name> - Clean.<ext>`` (see ``clean_output_path`` in ``worker/queue.py``).

This one-off migration moves such files into place — but only for films that are
in their own folder, where the version naming actually groups — and updates any
matching job record so the review page shows the new location. It is safe to run
more than once.

Dry run by default; pass ``--apply`` to actually move files::

    scripts/migrate-clean-copies.sh                       # dry run
    scripts/migrate-clean-copies.sh --apply               # move them
    scripts/migrate-clean-copies.sh --apply "\\\\Nas\\Movies"  # explicit root(s)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Iterator, Optional

from .review import media_roots
from .store import Store

CLEAN_SUFFIX = " (Clean)"


def _roots_from_worker() -> list[Path]:
    """Ask the running worker for its configured media roots.

    The Windows service bakes ``CLEANMEDIA_MEDIA_ROOTS`` into its own launcher,
    so a plain shell doesn't see it — but the worker does, and reports it on
    ``/api/health``. This lets the migration Just Work while the worker is up,
    with no path to type.
    """
    port = os.environ.get("PORT", "8765")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            data = json.load(resp)
        return [Path(r) for r in data.get("mediaRoots", [])]
    except Exception:
        return []


def resolve_roots(explicit: list[str]) -> list[Path]:
    """Where to scan: explicit args win, then the env, then the running worker."""
    if explicit:
        roots = [Path(r) for r in explicit]
    elif os.environ.get("CLEANMEDIA_MEDIA_ROOTS"):
        roots = media_roots()
    else:
        roots = _roots_from_worker() or media_roots()
    return [r for r in roots if r.is_dir()]


def _norm(p: str | Path) -> str:
    """Case- and separator-normalised path, for comparing worker vs stored paths."""
    return os.path.normcase(os.path.normpath(str(p)))


def plan(roots: list[Path]) -> Iterator[tuple[Path, Optional[Path]]]:
    """Yield ``(source_in_cleaned, target_version_path)`` for each clean copy.

    ``target`` is ``None`` when the film is not in its own folder, so the caller
    can report it as un-migratable (a flat library or a TV episode can't become
    a Jellyfin version).
    """
    for root in roots:
        try:
            cleaned_dirs = list(root.rglob("cleaned"))
        except OSError:
            continue
        for cleaned_dir in cleaned_dirs:
            if not cleaned_dir.is_dir():
                continue
            movie_folder = cleaned_dir.parent
            for src in sorted(cleaned_dir.iterdir()):
                if not src.is_file() or not src.stem.endswith(CLEAN_SUFFIX):
                    continue
                original_stem = src.stem[: -len(CLEAN_SUFFIX)]
                # Version grouping needs the file to sit in a folder named for
                # the film — the same rule clean_output_path keys on.
                if movie_folder.name != original_stem:
                    yield src, None
                    continue
                yield src, movie_folder / f"{movie_folder.name} - Clean{src.suffix}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Move clean copies out of cleaned/ into the '<movie> - Clean' version path.",
    )
    ap.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    ap.add_argument("roots", nargs="*", help="media root(s) to scan (default: CLEANMEDIA_MEDIA_ROOTS)")
    args = ap.parse_args(argv)

    roots = resolve_roots(args.roots)
    if not roots:
        print(
            "No usable media roots found.\n"
            "  - Start the worker (it reports its roots), or\n"
            "  - pass a path: scripts/migrate-clean-copies.sh '\\\\Nas\\nas-8tb-hdd\\Movies'\n"
            "    (use single quotes so the backslashes survive), or\n"
            "  - set CLEANMEDIA_MEDIA_ROOTS.",
            file=sys.stderr,
        )
        return 1
    print("Scanning:", ", ".join(str(r) for r in roots))

    store = Store()
    jobs = store.list_jobs() if args.apply else []
    moved = skipped = 0

    for src, target in plan(roots):
        if target is None:
            print(f"  skip (not its own folder): {src}")
            skipped += 1
            continue
        if target.exists():
            print(f"  skip (version already exists): {target}")
            skipped += 1
            continue
        if not args.apply:
            print(f"  would move: {src}\n          -> {target}")
            moved += 1
            continue

        src_norm = _norm(src)
        src.rename(target)
        # Keep any render job's recorded path honest, so the review page's Recent
        # list shows the new location rather than the old cleaned/ one.
        for job in jobs:
            if job.renderedPath and _norm(job.renderedPath) == src_norm:
                job.renderedPath = str(target)
                store.save_job(job)
        # Tidy up an emptied cleaned/ folder.
        try:
            next(src.parent.iterdir())
        except StopIteration:
            src.parent.rmdir()
        print(f"  moved: {src.name} -> {target.name}")
        moved += 1

    verb = "Moved" if args.apply else "Would move"
    noun = "copy" if moved == 1 else "copies"
    print(f"\n{verb} {moved} clean {noun}; skipped {skipped}.")
    if moved and not args.apply:
        print("Re-run with --apply to move them, then rescan your Jellyfin library.")
    elif moved and args.apply:
        print("Done. Rescan your Jellyfin library so the Clean versions attach.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
