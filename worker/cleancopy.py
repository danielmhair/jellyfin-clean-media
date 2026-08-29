"""Where a rendered clean copy goes, and how to recognise one again.

The naming is not cosmetic: Jellyfin only groups several files in a movie
folder as selectable *versions* of one movie when each name is the folder name
followed by ``" - <label>"``. Get it wrong and the clean copy shows up as a
duplicate movie instead of a "Clean" version in the player.

The other half is reading that name back. Reviewing the clean copy — not the
original — is the normal way a missed word gets found, so a clean copy is a
routine *input* to the next render, and the render has to keep its output in
the same version family rather than producing a "Clean (Clean)".

Kept apart from :mod:`worker.queue` so the review UI can share it without
pulling in the analysis engines.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional


#: The version label a rendered clean copy carries. Jellyfin shows whatever
#: follows ``" - "`` as the version name in the player, so the first copy is
#: "Clean" and any kept-alongside re-render is "Clean 2", "Clean 3", …
CLEAN_LABEL = "Clean"

#: "Clean", "Clean 2" — the label of a copy we rendered, and which one it is.
_CLEAN_LABEL_RE = re.compile(rf"^{re.escape(CLEAN_LABEL)}(?: (\d+))?$")
#: The legacy ``cleaned/`` form: "Some Film (2010) (Clean)", "… (Clean 2)".
_LEGACY_CLEAN_RE = re.compile(rf" \({re.escape(CLEAN_LABEL)}(?: (\d+))?\)$")


def _version_label(media: Path) -> Optional[str]:
    """``media``'s Jellyfin *version* label, or None if it isn't one.

    Jellyfin reads a file in a per-movie folder as a version of that movie when
    its name is the folder name followed by ``" - <label>"``; the label is what
    the player lists. ``Movie (2014)/Movie (2014) - Clean.mkv`` → ``"Clean"``.
    """
    prefix = media.parent.name + " - "
    return media.stem[len(prefix):] if media.stem.startswith(prefix) else None


def clean_copy_variant(media: Path) -> Optional[int]:
    """Which of our rendered clean copies ``media`` is — 1 for the first — or
    None when it is an ordinary source file.

    A clean copy is a perfectly good thing to review and re-render (finding a
    missed word while watching the clean version is the normal way this goes),
    so the render path has to recognise one as its *input* and keep the output
    in the same version family, rather than producing a "Clean (Clean)".
    """
    label = _version_label(media)
    if label is not None:
        matched = _CLEAN_LABEL_RE.match(label)
        if matched:
            return int(matched.group(1) or 1)
    # The pre-versions layout: a "cleaned/" subfolder beside a flat library.
    if media.parent.name == "cleaned":
        matched = _LEGACY_CLEAN_RE.search(media.stem)
        if matched:
            return int(matched.group(1) or 1)
    return None


def is_clean_copy(media: Path) -> bool:
    """Whether ``media`` is itself a clean copy this worker rendered."""
    return clean_copy_variant(media) is not None


def clean_output_path(media: Path, variant: int = 1) -> Path:
    """Where a film's clean copy should be written.

    Jellyfin groups multiple files in one *movie folder* as selectable versions
    of the same movie, as long as each file name begins — character for
    character — with the folder name, followed by ``" - <label>"``. So when the
    film sits in its own folder (``Movie (2014)/Movie (2014).mkv``) we write the
    clean copy alongside it as ``Movie (2014) - Clean.mkv``: it shows up as a
    "Clean" version to pick in the player, and the original is never touched.

    ``variant`` numbers a copy kept *alongside* an existing one — 2 gives
    ``Movie (2014) - Clean 2.mkv``, which lists as its own "Clean 2" version —
    so a re-render can spare the copy the administrator is currently watching.

    A clean copy handed back as the source stays in that family: re-rendering
    ``Movie (2014) - Clean.mkv`` targets ``Movie (2014) - Clean.mkv`` (replace)
    or ``Movie (2014) - Clean 2.mkv`` (a second copy), never a nested
    "Clean (Clean)".

    When the film is *not* in its own folder (a flat library, or an episode
    under ``Season 01/``), version grouping can't work — naming a file after the
    shared parent folder would either fail to group or collide — so we fall back
    to a ``cleaned/`` subfolder, which is safe but won't appear as a version.
    """
    label = CLEAN_LABEL if variant <= 1 else f"{CLEAN_LABEL} {variant}"
    folder = media.parent

    # A dedicated per-movie folder is the standard Jellyfin layout and the only
    # one where the file name equals the folder name. That exact match is also
    # what version grouping requires, so it's the right condition to key on —
    # as is a clean copy carrying that folder's version label, which means we
    # are already inside the family. (Only a *clean* label counts: in a flat
    # show folder, "Show - S01E01.mkv" is also folder-prefixed, and treating it
    # as a version would collide every episode onto one "Show - Clean.mkv".)
    if media.stem == folder.name or (
        is_clean_copy(media) and _version_label(media) is not None
    ):
        return folder / f"{folder.name} - {label}{media.suffix}"

    # Flat layout. A legacy clean copy re-renders into its own folder, with its
    # old label stripped, so copies accumulate as siblings rather than nesting.
    base_dir = folder if folder.name == "cleaned" else folder / "cleaned"
    stem = _LEGACY_CLEAN_RE.sub("", media.stem)
    return base_dir / f"{stem} ({label}){media.suffix}"


def next_clean_output_path(media: Path, limit: int = 50) -> Path:
    """The lowest-numbered clean-copy path that is not on disk yet.

    Used for "keep the existing copy, make another": a stat that throws (an
    unreachable share) counts the name as free — the render is about to fail on
    that share anyway, and guessing "taken" would silently skip a free slot.
    """
    for variant in range(1, limit + 1):
        candidate = clean_output_path(media, variant)
        try:
            if not candidate.exists():
                return candidate
        except OSError:
            return candidate
    raise ValueError(f"{limit} clean copies of {media.name} already exist")


def render_source(media: Path) -> Path:
    """The file a render should actually read.

    A clean copy renders from the film it came from, never from itself. The
    film plus today's approvals is the whole answer, so a re-render picks up
    every finding instead of stacking one edit on top of another — and it
    encodes the original once rather than re-encoding an encode.
    """
    return source_of(media) or media


def render_plan(media: Path) -> dict:
    """The two places a render of ``media`` could go, for the caller to choose.

    A clean copy is a real file someone may be part-way through watching, so a
    re-render must not silently take it out from under them. *Replace* rewrites
    the copy this render supersedes — the copy itself when it is what's being
    watched, otherwise the film's ``- Clean`` — and *new* writes the next free
    ``Clean N`` beside it, leaving the old copy playable.
    """
    replace = media if is_clean_copy(media) else clean_output_path(media, 1)
    try:
        replace_exists = replace.exists()
    except OSError:
        replace_exists = False
    new = next_clean_output_path(media)
    source = render_source(media)
    return {
        "mediaPath": str(media),
        "sourcePath": str(source),
        "sourceName": source.name,
        "sourceIsCleanCopy": is_clean_copy(media),
        "replacePath": str(replace),
        "replaceLabel": _version_label(replace) or replace.stem,
        "replaceExists": replace_exists,
        "newPath": str(new),
        "newLabel": _version_label(new) or new.stem,
    }


def render_target(media: Path, mode: str) -> Path:
    """Resolve a render ``mode`` ("replace"/"new") to the file to write."""
    plan = render_plan(media)
    return Path(plan["newPath"] if mode == "new" else plan["replacePath"])


# ---------------------------------------------------------------------------
# Which film a clean copy came from, and where a moment in it really is.
#
# Watching the clean copy is how a missed word gets found, so the review loop
# has to lead back from the copy to the film it was made from. Two things are
# needed: the source file, and the cuts the render removed — a flag at 43:45 in
# a copy with ten minutes cut out of it is not 43:45 in the original.
#
# Both are recorded beside the copy when it is written, because the sidecar's
# approvals go on changing afterwards and re-deriving the cuts from *today's*
# approvals would misplace every flag on a copy rendered before the last edit.

#: Written next to a clean copy: which film it came from, and what was cut.
ORIGIN_SUFFIX = ".cleanmedia-origin.json"

#: Containers we will accept as the original film when looking one up by name.
_VIDEO_SUFFIXES = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm")


def origin_record_for(clean: Path) -> Path:
    """Where a clean copy's origin record lives."""
    return clean.with_name(clean.stem + ORIGIN_SUFFIX)


def write_origin_record(clean: Path, source: Path, cuts: list[tuple[int, int]]) -> None:
    """Record what ``clean`` was rendered from, and the spans cut out of it.

    ``cuts`` are in *source* time. Best-effort: a share that refuses the write
    must not fail a render that already succeeded — the naming rule and today's
    approvals still give a usable fallback (see :func:`source_of`).
    """
    record = {
        "source": str(source),
        "cuts": [[int(a), int(b)] for a, b in sorted(cuts)],
    }
    try:
        origin_record_for(clean).write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_origin_record(clean: Path) -> Optional[dict]:
    """The origin record beside ``clean``, or None if it has none."""
    path = origin_record_for(clean)
    try:
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("source") else None


def source_of(clean: Path) -> Optional[Path]:
    """The film a clean copy was rendered from, or None if it isn't a copy.

    Prefers the recorded source; falls back to the naming rule, so a copy
    rendered before origin records existed still leads back to its film.
    """
    if not is_clean_copy(clean):
        return None

    record = read_origin_record(clean)
    if record is not None:
        recorded = Path(record["source"])
        if recorded.is_file():
            return recorded

    # Naming rule: "<folder>/<folder> - Clean.mkv" came from "<folder>/<folder>.*",
    # and the legacy "<dir>/cleaned/<name> (Clean).mkv" from "<dir>/<name>.*".
    if _version_label(clean) is not None:
        folder = clean.parent
        stem = folder.name
    else:
        folder = clean.parent.parent
        stem = _LEGACY_CLEAN_RE.sub("", clean.stem)

    same_suffix = folder / f"{stem}{clean.suffix}"
    if same_suffix.is_file():
        return same_suffix
    for suffix in _VIDEO_SUFFIXES:
        candidate = folder / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def cuts_of(clean: Path) -> list[tuple[int, int]]:
    """The source-time spans cut out of ``clean``, from its origin record.

    Empty when there is no record — which is also the right answer for the
    common case, since a mute or a blur leaves the timeline untouched and only
    a cut moves a moment.
    """
    record = read_origin_record(clean)
    if record is None:
        return []
    cuts: list[tuple[int, int]] = []
    for span in record.get("cuts") or []:
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        if end > start:
            cuts.append((start, end))
    return sorted(cuts)


def to_source_ms(ms: int, cuts: list[tuple[int, int]]) -> int:
    """Map a moment in a clean copy back to the same moment in the source.

    Every cut that lands at or before the position adds its length back. Walking
    the cuts in ascending order keeps the running position in source time, so
    each comparison is against a source-time cut start.
    """
    position = max(0, int(ms))
    for start, end in sorted(cuts):
        if position >= start:
            position += end - start
    return position
