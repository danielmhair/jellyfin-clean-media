"""Administrator review UI.

Serves a page of every finding for a film with a thumbnail, and writes
approve/reject decisions straight back to the `.cleanmedia.json` sidecar.

That sidecar is what `GET /api/segments` reads, and the Jellyfin plugin
requests `approvedOnly=true` — so approving a finding here is what makes
Jellyfin skip it. Rejecting it makes it disappear from playback and from
any future render. Nothing acts on a finding until it is approved.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from .cleancopy import (
    cuts_of,
    merge_spans,
    read_origin_record,
    source_of,
    to_source_ms,
)
from .logging_config import get_logger
from .models import Segment, Timeline
from .render import BLUR_SIGMA
from .settings import get_settings
from .store import media_fingerprint

logger = get_logger("review")

THUMB_WIDTH = 480
CLIP_PAD_S = 15.0

#: Review clips are short-lived scratch files a reviewer waits on live, so
#: speed matters more than the visual quality that ``render.py`` optimizes
#: for — GPU encode (proven working there, see ``use_nvenc``) turns the
#: blocking transcode from several seconds of CPU x264 into roughly one.
CLIP_VIDEO_ENCODE_ARGS = ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-cq", "26"]

#: Engine identity for findings an administrator added by hand. Never runs,
#: so a merge always keeps its segments.
MANUAL_ENGINE = "manual"


def sidecar_for(media: Path) -> Path:
    return media.with_name(media.stem + ".cleanmedia.json")


def _configured_media_roots() -> str:
    """The raw roots string driving media_roots() and the index staleness
    signature — kept as one function so both always see the same value.

    The settings store (edited from the plugin's Settings tab) wins when
    non-empty — an admin who explicitly set it there means it — else
    CLEANMEDIA_MEDIA_ROOTS (the pre-plugin way to set this), else empty
    (media_roots() falls back to the bundled movies/ test folder).
    """
    stored = get_settings().mediaRoots.strip()
    return stored or os.environ.get("CLEANMEDIA_MEDIA_ROOTS", "")


def media_roots() -> list[Path]:
    """Directories to search when a caller's path does not exist locally."""
    configured = _configured_media_roots()
    roots = [Path(p) for p in configured.split(os.pathsep) if p.strip()]
    if not roots:
        roots = [Path(__file__).resolve().parent.parent / "movies"]
    usable = []
    for r in roots:
        try:
            if r.is_dir():
                usable.append(r)
        except OSError:
            # An unreachable share (e.g. a task with no network credentials)
            # raises WinError 1312 rather than returning False. Skip it — one
            # bad root must not 500 every status/resolve request.
            continue
    return usable


def browse_dir(path: str = "") -> dict:
    """List subdirectories of ``path``, for the plugin's media-roots folder
    picker on the Settings tab.

    The plugin runs in a browser that is often on a different machine from
    the worker (and its filesystem) entirely, so a native OS file dialog or
    an HTML file-input can't work here — this mirrors how Sonarr/Radarr/Plex
    do it: browse the *server's* filesystem over the API instead. Only
    directories are listed (never files) since this exists purely to build a
    media-roots path.

    An empty ``path`` means "give me somewhere to start": every media root
    that currently resolves, plus platform-appropriate starting points (drive
    letters on Windows, ``/`` and the home directory elsewhere) — useful even
    before any media root is configured.
    """
    if not path:
        seen: set[str] = set()
        entries = []
        for root in media_roots():
            entries.append({"name": str(root), "path": str(root)})
            seen.add(str(root))
        if os.name == "nt":
            import string

            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if drive in seen:
                    continue
                try:
                    # A stale/disconnected mapped network drive raises
                    # OSError (e.g. WinError 1326, bad cached credentials)
                    # rather than just returning False here — unlike a
                    # missing local drive, which is_dir() handles quietly.
                    if Path(drive).is_dir():
                        entries.append({"name": drive, "path": drive})
                except OSError:
                    continue
        else:
            for extra in ("/", str(Path.home())):
                try:
                    if extra not in seen and Path(extra).is_dir():
                        entries.append({"name": extra, "path": extra})
                except OSError:
                    continue
        return {"path": "", "parent": None, "dirs": entries}

    target = Path(path)
    try:
        is_dir = target.is_dir()
    except OSError as exc:
        raise NotADirectoryError(path) from exc
    if not is_dir:
        raise NotADirectoryError(path)
    dirs = []
    try:
        for child in sorted(target.iterdir(), key=lambda c: c.name.lower()):
            try:
                if child.is_dir() and not child.name.startswith("."):
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue  # a broken symlink or permission error, skip it
    except PermissionError:
        pass  # an unreadable directory still gets its parent link back
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "dirs": dirs}


# Filename -> path index over the media roots. Building it walks every root,
# which is cheap for a handful of test files but expensive over a real NAS
# library (thousands of files, often on SMB) — and the review grid resolves
# many paths per page. So walk once and cache; a stale entry (a film moved or
# removed) is caught by the is_file() recheck at lookup, and new films appear
# after the TTL. Guarded by a lock so concurrent requests don't each rebuild.
# 30 min: a media library changes rarely, and walking a large NAS share over
# SMB costs ~20s+, so a short TTL would stall the grid periodically. A newly
# added film shows up within this window, or immediately on a worker restart.
_INDEX_TTL_S = 1800.0
_index_lock = threading.Lock()
_index_cache: Optional[dict[str, Path]] = None
_sidecar_cache: Optional[set[str]] = None
_index_built_at = 0.0
#: The configured roots string (settings store or CLEANMEDIA_MEDIA_ROOTS) the
#: cache was built for. Comparing this (a cheap string, no I/O) means a roots
#: change — from the plugin, the env var, or a different root per test —
#: rebuilds the index instead of serving a stale one.
_index_roots_sig: Optional[str] = None


def _build_media_index() -> tuple[dict[str, Path], set[str]]:
    """Walk the media roots once, recording both the filename→path map (for
    resolving Jellyfin paths) and the set of sidecar filenames present (so the
    review grid can tell analyzed from not without a per-film stat).

    Uses os.walk, not Path.rglob + is_file(): rglob stats every entry, which is
    a NAS round trip per file and makes a large share take minutes. os.walk
    classifies entries from the directory listing with no extra stat.
    """
    index: dict[str, Path] = {}
    sidecars: set[str] = set()
    for root in media_roots():
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                base = Path(dirpath)
                for name in filenames:
                    low = name.lower()
                    if low.endswith(".cleanmedia.json"):
                        sidecars.add(low)
                    else:
                        index.setdefault(low, base / name)  # first match wins
        except OSError:
            continue  # a share that drops mid-walk must not 500 the request
    return index, sidecars


def _ensure_index(refresh: bool = False) -> tuple[dict[str, Path], set[str]]:
    global _index_cache, _sidecar_cache, _index_built_at, _index_roots_sig
    with _index_lock:
        sig = _configured_media_roots()
        stale = (
            _index_cache is None
            or sig != _index_roots_sig
            or (time.monotonic() - _index_built_at) > _INDEX_TTL_S
        )
        if refresh or stale:
            _index_cache, _sidecar_cache = _build_media_index()
            _index_built_at = time.monotonic()
            _index_roots_sig = sig
        return _index_cache, _sidecar_cache


def _media_index(refresh: bool = False) -> dict[str, Path]:
    index, _ = _ensure_index(refresh)
    return index


def sidecar_exists(media: Path) -> bool:
    """Whether a film has an analysis sidecar — answered from the cached index,
    with no NAS round trip. Lets the review grid skip a per-film stat for the
    (common) unanalyzed case."""
    _, sidecars = _ensure_index()
    return sidecar_for(media).name.lower() in sidecars


def warm_media_index() -> None:
    """Build the media index now, so the first review-grid load doesn't wait on
    a cold walk of a large (often NAS/SMB) media root. Safe to call from a
    background thread at startup."""
    _ensure_index(refresh=True)


def resolve_media(path: str) -> Optional[Path]:
    """Map a caller's path onto a local file.

    Jellyfin knows a film as /volume1/Media/Movies/Film.mkv while the
    worker has C:/media/Film.mkv — the same movie, different mount. Rather
    than make administrators maintain a path-mapping table, fall back to
    matching the file name inside the configured media roots (see the cached
    index above).
    """
    candidate = Path(path)
    if candidate.is_file():
        return candidate

    wanted = PurePosixPath(path.replace("\\", "/")).name.lower()
    if not wanted:
        return None

    hit = _media_index().get(wanted)
    if hit is not None and hit.is_file():
        return hit
    return None


def review_target(media: Path) -> Path:
    """The file whose findings a review of ``media`` should show.

    Opening a clean copy — from a stale link, or the player's "review this
    film" button while the copy is what's playing — normally shows the film
    instead: a copy is rebuilt from the film's approvals on every render, so
    the film is where a *new* decision has to land to survive. The one
    exception is a copy that already holds findings of its own — flags made
    before create_segment started redirecting to the source automatically
    are stuck there and were deliberately left in place rather than
    guess-migrated (a render can desync timestamps through cuts with no
    origin record to map them back precisely). Reviewing such a copy has to
    show *its own* timeline, or the findings it actually holds are
    unreachable — bounced to a film page that doesn't have them.
    """
    source = source_of(media)
    if source is None:
        return media
    own = load_timeline(media)
    if own is not None and own.segments:
        return media
    return source


def load_timeline(media: Path) -> Optional[Timeline]:
    path = sidecar_for(media)
    if not path.is_file():
        return None
    return Timeline.model_validate_json(path.read_text(encoding="utf-8"))


def save_timeline(media: Path, timeline: Timeline) -> None:
    """Write the sidecar. This is the record of every review decision.

    Written to a temp file in the same directory and swapped in with
    ``os.replace`` (atomic on both POSIX and Windows), rather than truncating
    the sidecar in place — a save interrupted mid-write (worker restart, a
    dropped network-share write) must never leave a half-written or
    corrupted file behind, since that's every review decision made on the
    film so far.
    """
    path = sidecar_for(media)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(timeline.model_dump(), indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise
    _invalidate_summary(media)  # its review counts changed


# ---------- library view (the top-left video switcher) ----------

#: Video containers we treat as reviewable films (the media index also holds
#: sidecars, subtitles, artwork — filter to these).
VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm", ".wmv", ".mpg", ".mpeg",
}

# Per-film review summary {count, undecided}, cached in memory and dropped on any
# write to that film's sidecar (save_timeline). Reading a small sidecar is cheap;
# caching spares the (often NAS) share on every library refresh, without the
# per-file stat that made the old status path slow.
_summary_cache: dict[str, dict] = {}
_summary_lock = threading.Lock()


def _invalidate_summary(media: Path) -> None:
    with _summary_lock:
        _summary_cache.pop(str(media), None)


def _film_summary(media: Path) -> dict:
    key = str(media)
    with _summary_lock:
        cached = _summary_cache.get(key)
    if cached is not None:
        return cached
    # A single malformed sidecar (hand-edited, or a write cut short) must not
    # 500 the whole library listing — every other film is still reviewable.
    # Surface it as its own status instead of hiding or crashing on it.
    try:
        tl = load_timeline(media)
    except Exception:
        logger.exception("unreadable sidecar for %s — flagging as corrupt", media)
        summ = {"count": 0, "undecided": 0, "corrupt": True}
    else:
        if tl is None:
            summ = {"count": 0, "undecided": 0}
        else:
            summ = {
                "count": len(tl.segments),
                "undecided": sum(1 for s in tl.segments if s.approved is None),
            }
    with _summary_lock:
        _summary_cache[key] = summ
    return summ


def _film_status(analyzed: bool, count: int, undecided: int, corrupt: bool = False) -> str:
    if corrupt:
        return "corrupt"               # sidecar exists but failed to parse
    if not analyzed:
        return "unanalyzed"            # no sidecar — search-only, opens for manual review
    if undecided <= 0:
        return "reviewed"             # every finding decided (or analysis found none)
    if undecided == count:
        return "ready"                # analyzed, nothing decided yet
    return "in_progress"              # part-way through


def _subseq(q: str, s: str) -> bool:
    """A loose fuzzy match: are q's chars an in-order subsequence of s?"""
    it = iter(s)
    return all(c in it for c in q)


def _match_rank(q: str, name: str) -> int:
    if name.startswith(q):
        return 0
    if q in name:
        return 1
    return 2  # subsequence (already filtered to matches)


#: Order the default work-list puts statuses in: needs-your-attention first.
_STATUS_ORDER = {"ready": 0, "in_progress": 1, "corrupt": 2, "reviewed": 3, "unanalyzed": 4}


def library_view(query: str = "", limit: int = 50, offset: int = 0) -> dict:
    """The top-left switcher's data. With no ``query`` it is the review work-list
    — analyzed films, needs-review first (most-undecided first), then reviewed.
    With a ``query`` it fuzzy-matches **every** video in every collection
    (including unanalyzed ones, which open for manual review), ranked by match.

    Rendered clean copies list alongside their film like any other video —
    a copy is normally just an output with nothing of its own to review (see
    :func:`review_target`, which redirects opening one to the film), but a
    copy that already holds findings of its own needs to be reachable to
    review them, and the grid is how a reviewer gets back to it.
    """
    index, sidecars = _ensure_index()
    q = query.strip().lower()
    items: list[dict] = []
    for name, path in index.items():
        if path.suffix.lower() not in VIDEO_EXTS:
            continue
        analyzed = sidecar_for(path).name.lower() in sidecars
        if not q and not analyzed:
            continue  # default list is analyzed-only; the untouched library is search-only
        if q and q not in name and not _subseq(q, name):
            continue
        summ = _film_summary(path) if analyzed else {"count": 0, "undecided": 0}
        status = _film_status(analyzed, summ["count"], summ["undecided"], summ.get("corrupt", False))
        items.append({
            "path": str(path),
            "name": path.stem,
            "collection": path.parent.name,
            "status": status,
            "findingCount": summ["count"],
            "undecidedCount": summ["undecided"],
        })
    if q:
        items.sort(key=lambda it: (_match_rank(q, it["name"].lower()), it["name"].lower()))
    else:
        items.sort(key=lambda it: (
            _STATUS_ORDER[it["status"]], -it["undecidedCount"], it["name"].lower(),
        ))
    return {"total": len(items), "items": items[offset : offset + limit]}


def next_segment_id(segments: list[Segment], floor: int = 0) -> int:
    """Ids are allocated monotonically and never reused.

    Renumbering would be tidier on disk but changes the id of a finding
    an administrator may be looking at, or has open in another tab. The
    floor is the timeline's own high-water mark, which keeps that true
    even after the highest-numbered finding is deleted.
    """
    return max(max((s.id for s in segments), default=0) + 1, floor)


def set_approval(media: Path, segment_id: int, approved: Optional[bool]) -> bool:
    """Persist a decision. Returns False if the segment does not exist."""
    return update_segment(media, segment_id, approved=approved) is not None


def set_approvals(
    media: Path, segment_ids: list[int], approved: Optional[bool]
) -> int:
    """Apply one decision to many findings in a single write.

    Returns the number of findings actually changed. Doing this as one
    load/save rather than a PATCH per finding is not just faster: N
    concurrent single-segment writes each read-modify-write the same
    sidecar, so the last to land would clobber the others' decisions.
    Unknown ids are ignored — a bulk action should not fail because one
    finding was deleted in another tab.
    """
    timeline = load_timeline(media)
    if timeline is None:
        return 0
    wanted = set(segment_ids)
    changed = 0
    for segment in timeline.segments:
        if segment.id in wanted:
            segment.approved = approved
            changed += 1
    if changed:
        save_timeline(media, timeline)
    return changed


def update_segment(
    media: Path,
    segment_id: int,
    *,
    approved=...,
    start_ms=...,
    end_ms=...,
    action=...,
    reasoning=...,
    category=...,
) -> Optional[Segment]:
    """Edit one finding in place. Returns None if it does not exist.

    Sentinel defaults rather than None, because None is a meaningful value
    for `approved` — it means "no decision yet".
    """
    timeline = load_timeline(media)
    if timeline is None:
        return None
    for segment in timeline.segments:
        if segment.id != segment_id:
            continue
        if approved is not ...:
            segment.approved = approved
        if start_ms is not ...:
            segment.startMs = max(0, int(start_ms))
        if end_ms is not ...:
            segment.endMs = max(0, int(end_ms))
        if action is not ...:
            segment.recommendedAction = action
        if reasoning is not ...:
            segment.reasoning = reasoning
        if category is not ...:
            segment.category = category
        # An inverted span would silently skip nothing, or everything.
        if segment.endMs < segment.startMs:
            segment.startMs, segment.endMs = segment.endMs, segment.startMs
        save_timeline(media, timeline)
        return segment
    return None


def delete_segment(media: Path, segment_id: int) -> bool:
    """Remove a finding. Survivors keep their ids."""
    timeline = load_timeline(media)
    if timeline is None:
        return False
    remaining = [s for s in timeline.segments if s.id != segment_id]
    if len(remaining) == len(timeline.segments):
        return False
    timeline.segments = remaining
    save_timeline(media, timeline)
    return True


#: A copy and its film count as the same length within this much. A re-encode
#: moves the duration by a frame or two, and containers round; nothing near the
#: length of a real cut.
_SAME_LENGTH_MS = 1_500

#: How closely the film's approved skips must add up to the footage actually
#: missing from a copy before those skips are trusted as that copy's cut list.
_CUTS_MATCH_MS = 2_000


def _duration_ms(media: Path) -> Optional[int]:
    """The file's length in ms, or None when it can't be probed."""
    from .shots import media_duration

    try:
        seconds = media_duration(media)
    except Exception:
        return None  # an unreachable share or an unreadable file: just unknown
    return int(seconds * 1000) if seconds and seconds > 0 else None


def _inferred_cuts(copy: Path, film: Path) -> tuple[list[tuple[int, int]], bool]:
    """What a copy with no origin record had cut out of it, and whether to trust it.

    Copies rendered before origin records existed have to be placed some other
    way, and guessing from today's approvals is not safe on its own — approvals
    change after a render, and every changed one moves the answer.

    So measure instead of assume. If the copy is the same length as the film,
    nothing was cut and its clock is the film's clock, whatever the sidecar now
    says. If it is shorter, the film's approved skips are only believed when
    they add up to the footage actually missing. When they don't, the cuts are
    unknown and the caller is told so rather than handed a plausible wrong
    number.
    """
    copy_ms, film_ms = _duration_ms(copy), _duration_ms(film)
    if copy_ms is None or film_ms is None:
        return [], False

    missing = film_ms - copy_ms
    if missing <= _SAME_LENGTH_MS:
        return [], True  # nothing was cut — a mute or a blur moves no timings

    timeline = load_timeline(film)
    skips = merge_spans(
        (s.startMs, s.endMs)
        for s in (timeline.segments if timeline else [])
        if s.approved is True and s.recommendedAction == "skip"
    )
    accounted = sum(end - start for start, end in skips)
    if skips and abs(accounted - missing) <= _CUTS_MATCH_MS:
        return skips, True
    return [], False


def _redirect_to_source(
    media: Path,
    start_ms: int,
    end_ms: int,
    reasoning: Optional[str],
    approved: Optional[bool],
) -> tuple[Path, int, int, Optional[str], Optional[bool]]:
    """Move a hand-added finding from a clean copy onto the film it came from.

    Returns the arguments unchanged for an ordinary file, or for a copy whose
    film can no longer be found — better a finding on the copy than a finding
    dropped.

    Cuts shift everything after them, so the flagged time is mapped back into
    film time. The copy's origin record, written when it was rendered, makes
    that exact. Without one — every copy rendered before those records existed —
    the mapping is inferred from the two files' durations, and if it cannot be
    established the finding still goes on the film (that is the only place it
    survives a render) but is left *undecided* and says why. A finding whose
    timing nobody can vouch for must not arrive pre-approved: acting on it would
    mute or cut the wrong moment.
    """
    source = source_of(media)
    if source is None or source == media:
        return media, start_ms, end_ms, reasoning, approved

    if read_origin_record(media) is not None:
        cuts, trusted = cuts_of(media), True
    else:
        cuts, trusted = _inferred_cuts(media, source)

    note = f"flagged on {media.name}"
    if not trusted:
        note += (
            " — timing unverified: that copy is shorter than the film and its "
            "cuts are not on record, so check where this lands before approving"
        )
        approved = None

    return (
        source,
        to_source_ms(start_ms, cuts),
        to_source_ms(end_ms, cuts),
        f"{reasoning} ({note})" if reasoning else note,
        approved,
    )


def create_segment(
    media: Path,
    start_ms: int,
    end_ms: int,
    category: str,
    action: str,
    approved: Optional[bool] = True,
    reasoning: Optional[str] = None,
) -> Optional[Segment]:
    """Add a finding by hand.

    Marked with the MANUAL_ENGINE identity so re-analysis can merge fresh
    detections without discarding it — an administrator's own judgement
    must outlive any model's.

    Defaults to approved: adding a finding deliberately *is* the decision.

    Flagged while watching a *clean copy* — the usual way a missed word turns
    up — the finding is written against the film instead, at the matching
    moment in it. A copy is rebuilt from the film's approvals on every render,
    so a finding recorded on the copy would be wiped by the next one; and
    because a render's cuts shorten the copy, the flagged time is shifted back
    through them rather than taken at face value. Where that shift cannot be
    established, the finding arrives undecided instead of approved — see
    :func:`_redirect_to_source`.
    """
    media, start_ms, end_ms, reasoning, approved = _redirect_to_source(
        media, start_ms, end_ms, reasoning, approved
    )
    timeline = load_timeline(media)
    if timeline is None:
        # First finding on a film nothing has analyzed yet.
        try:
            fingerprint = media_fingerprint(media)
        except OSError:
            return None
        timeline = Timeline(mediaFingerprint=fingerprint, segments=[])

    start_ms, end_ms = sorted((max(0, int(start_ms)), max(0, int(end_ms))))
    segment_id = next_segment_id(timeline.segments, timeline.nextSegmentId)
    timeline.nextSegmentId = segment_id + 1
    segment = Segment(
        id=segment_id,
        startMs=start_ms,
        endMs=end_ms,
        category=category,
        confidence=1.0,  # a human said so
        engine=MANUAL_ENGINE,
        recommendedAction=action,
        approved=approved,
        reasoning=reasoning,
    )
    timeline.segments.append(segment)
    timeline.segments.sort(key=lambda s: s.startMs)
    save_timeline(media, timeline)
    return segment


# Category severity for labelling a merged finding: the worst one wins.
_MERGE_SEVERITY = {
    "nudity": 5,
    "gore": 5,
    "sexual": 4,
    "sex": 4,
    "violence": 3,
    "suggestive": 2,
    "profanity": 1,
}


def merge_into_one(
    media: Path,
    ids: list[int],
    action: str = "skip",
    approved: Optional[bool] = True,
) -> Optional[Segment]:
    """Combine several findings into a single one spanning them all.

    The merged finding runs from the earliest start to the latest end of the
    selected findings, so a run of adjacent detections — a whole scene the
    engine flagged shot by shot — becomes one segment to skip. The originals are
    removed; the merged finding is marked MANUAL_ENGINE so a re-analysis will
    not erase this administrator decision, and defaults to an approved skip
    because merging is itself the decision. Its category is the most severe of
    the sources. Returns the new segment, or None if fewer than two of the ids
    exist.
    """
    timeline = load_timeline(media)
    if timeline is None:
        return None
    wanted = set(ids)
    chosen = [s for s in timeline.segments if s.id in wanted]
    if len(chosen) < 2:
        return None

    start = min(s.startMs for s in chosen)
    end = max(s.endMs for s in chosen)
    category = max(chosen, key=lambda s: _MERGE_SEVERITY.get(s.category, 0)).category
    labels = ", ".join(f"#{s.id}" for s in sorted(chosen, key=lambda s: s.startMs))

    remaining = [s for s in timeline.segments if s.id not in wanted]
    segment_id = next_segment_id(remaining, timeline.nextSegmentId)
    timeline.nextSegmentId = segment_id + 1
    merged = Segment(
        id=segment_id,
        startMs=start,
        endMs=end,
        category=category,
        confidence=1.0,  # a human deliberately grouped these
        engine=MANUAL_ENGINE,
        recommendedAction=action,
        approved=approved,
        reasoning=f"merged {len(chosen)} findings ({labels})",
    )
    remaining.append(merged)
    remaining.sort(key=lambda s: s.startMs)
    timeline.segments = remaining
    save_timeline(media, timeline)
    return merged


def merge_segments(
    prior: list[Segment],
    fresh: list[Segment],
    replacing: set[str],
    floor: int = 0,
) -> list[Segment]:
    """Fold a fresh analysis into what is already on disk.

    Two rules, both about not destroying work:

    * Findings from engines that did not just run are kept — including
      MANUAL_ENGINE, which never runs. Re-running the visual pass must not
      erase profanity results or an administrator's own additions.
    * A decision already made carries over onto the matching fresh
      finding, matched on the engine's own reference. Re-analysis should
      not send a reviewer back through work they already did.

    Ids are preserved for survivors; fresh findings are allocated above the
    high-water mark rather than renumbering the lot.
    """
    kept = [s for s in prior if s.engine not in replacing or s.engine == MANUAL_ENGINE]
    decisions = {
        (s.engine, s.engineRef, s.category): s.approved
        for s in prior
        if s.engineRef is not None and s.approved is not None
    }

    next_id = next_segment_id(kept, floor)
    # Copy: callers hold these objects (the job's own timeline), and
    # reassigning their ids underneath them would be a nasty surprise.
    for segment in (s.model_copy(deep=True) for s in fresh):
        prior_decision = decisions.get((segment.engine, segment.engineRef, segment.category))
        if prior_decision is not None:
            segment.approved = prior_decision
        segment.id = next_id
        next_id += 1
        kept.append(segment)

    kept.sort(key=lambda s: s.startMs)
    return kept


def clip_path(
    media: Path, start_ms: int, end_ms: int, pad_s: float, mute: bool, voice: bool = False
) -> Path:
    """Cache location for a review clip; regenerating one is a few seconds."""
    key = hashlib.sha256(
        f"{media}|{start_ms}|{end_ms}|{pad_s}|{mute}|{voice}".encode("utf-8")
    ).hexdigest()[:20]
    cache = Path(tempfile.gettempdir()) / "cleanmedia-clips"
    cache.mkdir(exist_ok=True)
    return cache / f"{key}.mp4"


def _merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _window_keeps(cuts: list[tuple[float, float]], dur: float) -> list[tuple[float, float]]:
    """Invert cut spans into the spans to keep across [0, dur] (seconds)."""
    keeps: list[tuple[float, float]] = []
    prev = 0.0
    for a, b in cuts:
        if a > prev:
            keeps.append((prev, a))
        prev = max(prev, b)
    if prev < dur:
        keeps.append((prev, dur))
    return keeps


def preview_clip_path(
    media: Path, win_start_ms: int, win_end_ms: int,
    cut_rel: list[tuple[float, float]], mute_rel: list[tuple[float, float]],
    blur_rel: list[tuple[float, float]], voice_rel: list[tuple[float, float]],
) -> Path:
    """Cache location for a cleaned-window preview, keyed by its decisions so a
    change (approve another finding, retime one, blur one) yields a different file."""
    sig = f"{media}|{win_start_ms}|{win_end_ms}|{cut_rel}|{mute_rel}|{blur_rel}|{voice_rel}"
    key = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:20]
    cache = Path(tempfile.gettempdir()) / "cleanmedia-clips"
    cache.mkdir(exist_ok=True)
    return cache / f"preview-{key}.mp4"


def _blur_vf(blur_rel: list[tuple[float, float]]) -> str:
    """Leading video filter that blurs the frame across the given (relative-second)
    spans — the same full-frame ``gblur`` the render applies, so the Cleaned
    preview shows a blur exactly where the clean copy will have one."""
    if not blur_rel:
        return ""
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in blur_rel)
    return f"gblur=sigma={BLUR_SIGMA}:enable='{expr}',"


def build_preview_clip(
    media: Path,
    win_start_ms: int,
    win_end_ms: int,
    cuts: list[tuple[int, int]],
    mutes: list[tuple[int, int]],
    blurs: Optional[list[tuple[int, int]]] = None,
    voices: Optional[list[tuple[int, int]]] = None,
) -> Optional[Path]:
    """A short *cleaned* preview of a window: approved **skips are cut out** (their
    footage is never transcoded — the whole point vs. encoding then jumping),
    approved **mutes are silenced**, approved **voice-only mutes have just their
    vocals removed** (Demucs — the music/ambient plays through), and approved
    **blurs are blurred** (the render's full-frame ``gblur``), so a reviewer
    sees/hears the window as the viewer will. ``cuts`` / ``mutes`` / ``blurs`` /
    ``voices`` are absolute-ms spans; the window is [win_start_ms, win_end_ms].
    Returns None on failure.

    Voice removal runs one Demucs pass over the window (cached like the rest of
    the clip), so this scene preview shows the true voice-only mute — unlike the
    live whole-film stream, which can't separate per-frame and hard-mutes voice.
    """
    win_start = max(0.0, win_start_ms / 1000)
    win_dur = max(0.1, (win_end_ms - win_start_ms) / 1000)

    def rel(spans: list[tuple[int, int]]) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for a, b in spans:
            ra = max(0.0, a / 1000 - win_start)
            rb = min(win_dur, b / 1000 - win_start)
            if rb - ra > 0.02:
                out.append((ra, rb))
        return out

    cut_rel = _merge_spans(rel(cuts))
    mute_rel = rel(mutes)
    blur_rel = rel(blurs or [])
    voice_rel = rel(voices or [])

    out = preview_clip_path(
        media, win_start_ms, win_end_ms, cut_rel, mute_rel, blur_rel, voice_rel
    )
    if out.is_file() and out.stat().st_size > 0:
        return out

    # Voice-only findings: pre-render the window's audio with just the vocals
    # removed across those spans, and feed that as a second input in place of the
    # source audio. Everything downstream (hard mutes, the skip concat) then
    # operates on the voice-removed track.
    voice_wav: Optional[Path] = None
    if voice_rel:
        from .engines.voice_render import voice_removed_window_wav

        voice_wav = out.with_suffix(".voice.wav")
        made = voice_removed_window_wav(
            media, win_start, win_dur,
            [(win_start + a, win_start + b) for a, b in voice_rel],
            voice_wav,
        )
        if made is None:  # separation failed — fall back to a plain silence
            voice_wav = None

    keeps = _window_keeps(cut_rel, win_dur)
    if not keeps:  # the whole window is cut — keep a sliver so the element loads
        keeps = [(0.0, min(0.2, win_dur))]

    n = len(keeps)
    # When the vocals were removed the audio comes from input 1 (the WAV); the
    # video always comes from input 0, so skip-cut the voice spans there too.
    aidx = 1 if voice_wav else 0
    parts: list[str] = [f"[0:v]{_blur_vf(blur_rel)}scale=640:-2,split={n}" + "".join(f"[vs{i}]" for i in range(n))]
    asrc = f"[{aidx}:a]"
    if mute_rel:
        expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in mute_rel)
        parts.append(f"[{aidx}:a]volume=enable='{expr}':volume=0[am]")
        asrc = "[am]"
    parts.append(f"{asrc}asplit={n}" + "".join(f"[as{i}]" for i in range(n)))
    for i, (s, e) in enumerate(keeps):
        parts.append(f"[vs{i}]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[as{i}]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
    joins = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{joins}concat=n={n}:v=1:a=1[outv][outa]")
    filter_complex = ";".join(parts)

    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{win_start:.3f}", "-i", str(media)]
    if voice_wav:
        # The WAV is already just this window, so it needs no -ss seek.
        cmd += ["-i", str(voice_wav)]
    # Output-side -t bounds the whole clip; ffmpeg then stops reading input 0
    # once the (skip-trimmed) output ends, so the rest of the film is never read.
    cmd += [
        "-t", f"{win_dur:.3f}",
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        *CLIP_VIDEO_ENCODE_ARGS,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    finally:
        if voice_wav:
            voice_wav.unlink(missing_ok=True)
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        return None
    return out


def build_clip(
    media: Path,
    start_ms: int,
    end_ms: int,
    pad_s: float = 15.0,
    mute: bool = False,
    voice: bool = False,
) -> Optional[Path]:
    """Extract the flagged span plus padding, transcoded for the browser.

    DVD sources are MPEG-2, which browsers will not play, so this always
    re-encodes. Clips are short and cached, so the cost is paid once per
    finding no matter how often it is replayed.

    With ``mute`` the flagged span itself is silenced, so a reviewer can hear
    the scene exactly as it will play once the finding is acted on — the way
    to confirm a word's timing lands on the word and not the syllable beside
    it. The muted window is the finding's own span, offset by the padding
    that precedes it in the clip.

    With ``voice`` the span's **vocals** are removed (Demucs) instead of the
    whole audio, so a reviewer can hear the voice-only mute — the word gone,
    the music playing through — before approving it. Render-only either way.
    """
    out = clip_path(media, start_ms, end_ms, pad_s, mute, voice)
    if out.is_file() and out.stat().st_size > 0:
        return out

    start = max(0.0, start_ms / 1000 - pad_s)
    duration = (end_ms - start_ms) / 1000 + 2 * pad_s

    if voice:
        return _build_voice_clip(media, start, duration, start_ms, end_ms, out)

    audio_args = ["-c:a", "aac", "-b:a", "128k"]
    if mute:
        # Where the flagged span sits inside the padded clip. When the finding
        # is near the file start there is less than pad_s of lead-in, so the
        # offset is measured from the clip's actual start, not a fixed pad.
        lead = start_ms / 1000 - start
        mute_start = max(0.0, lead)
        mute_end = lead + (end_ms - start_ms) / 1000
        audio_args = [
            "-af",
            f"volume=enable='between(t,{mute_start:.3f},{mute_end:.3f})':volume=0",
            "-c:a", "aac", "-b:a", "128k",
        ]

    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(media), "-t", f"{duration:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=640:-2",
            *CLIP_VIDEO_ENCODE_ARGS,
            *audio_args,
            "-movflags", "+faststart", str(out),
        ],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        return None
    return out


def _build_voice_clip(
    media: Path, start: float, duration: float, start_ms: int, end_ms: int, out: Path
) -> Optional[Path]:
    """Voice-removed review clip: separate the vocals from the padded window,
    zero them across the flagged span, and mux that audio over the video."""
    from .engines.voice_render import voice_removed_wav

    wav = out.with_suffix(".wav")
    made = voice_removed_wav(
        media, start, duration, start_ms / 1000, end_ms / 1000, wav
    )
    if made is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-ss", f"{start:.3f}", "-i", str(media), "-t", f"{duration:.3f}",
                "-i", str(wav),
                "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                "-vf", "scale=640:-2",
                *CLIP_VIDEO_ENCODE_ARGS,
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out),
            ],
            capture_output=True, text=True, errors="replace",
        )
    finally:
        wav.unlink(missing_ok=True)
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        return None
    return out


def scrub_audio_path(media: Path, start_ms: int, end_ms: int) -> Path:
    key = hashlib.sha256(f"{media}|{start_ms}|{end_ms}|scrub".encode("utf-8")).hexdigest()[:20]
    cache = Path(tempfile.gettempdir()) / "cleanmedia-clips"
    cache.mkdir(exist_ok=True)
    return cache / f"scrub-{key}.wav"


def build_scrub_audio(media: Path, start_ms: int, end_ms: int) -> Optional[Path]:
    """A compact mono WAV of the window [start_ms, end_ms], for **live scrub
    audio**: the page decodes it once with WebAudio and then plays short grains at
    the drag position, so a reviewer hears the film as they drag the playhead —
    the way to locate an edit point by ear. Cheap to extract (audio only,
    downmixed to 22 kHz mono) and cached, so a scrub session hits it once.
    """
    start = max(0.0, start_ms / 1000)
    dur = max(0.05, (end_ms - start_ms) / 1000)
    out = scrub_audio_path(media, start_ms, end_ms)
    if out.is_file() and out.stat().st_size > 0:
        return out
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(media),
         "-t", f"{dur:.3f}", "-vn", "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le", str(out)],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        return None
    return out


def stream_command(
    media: Path,
    start_ms: int,
    runtime_ms: int,
    cuts: list[tuple[int, int]],
    mutes: list[tuple[int, int]],
    blurs: Optional[list[tuple[int, int]]] = None,
) -> list[str]:
    """ffmpeg argv to transcode the film from ``start_ms`` to the end into a
    **fragmented MP4 on stdout** (``pipe:1``) — a live stream a browser ``<video>``
    plays continuously across scenes, the Phase-2 whole-film counterpart to the
    per-scene ``build_clip``.

    With no cuts/mutes it is a straight transcode (Normal; the page mutes the
    element for Muted, so Muted reuses this same stream). With them (Cleaned) the
    cut spans are **removed** — their footage never transcoded, a whole-film
    version of ``build_preview_clip`` — and mutes silenced; the stream is then
    time-compressed and the page maps stream-time→film-time through the same
    keeps (``D_buildKeeps`` mirrors ``_merge_spans`` + ``_window_keeps``).

    Fragmented MP4 (``frag_keyframe+empty_moov``) needs no seekable output, so
    ffmpeg emits fragments as it encodes: playback starts in a second or two even
    though the encode runs to the end of the film. Seeking is the page's job (it
    reloads the stream from a new ``start_ms``), so no output-side seeking is
    required.
    """
    start = max(0.0, start_ms / 1000)
    dur = max(0.1, (runtime_ms - start_ms) / 1000)
    tail = [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    head = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(media), "-t", f"{dur:.3f}"]

    def rel(spans: list[tuple[int, int]]) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for a, b in spans:
            ra = max(0.0, a / 1000 - start)
            rb = min(dur, b / 1000 - start)
            if rb - ra > 0.02:
                out.append((ra, rb))
        return out

    cut_rel = _merge_spans(rel(cuts))
    mute_rel = rel(mutes)
    blur_rel = rel(blurs or [])

    if not cut_rel and not mute_rel and not blur_rel:  # Normal/Muted — no filtergraph
        return [*head, "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=640:-2", *tail]

    keeps = _window_keeps(cut_rel, dur) or [(0.0, min(0.2, dur))]
    n = len(keeps)
    parts: list[str] = [f"[0:v]{_blur_vf(blur_rel)}scale=640:-2,split={n}" + "".join(f"[vs{i}]" for i in range(n))]
    asrc = "[0:a]"
    if mute_rel:
        expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in mute_rel)
        parts.append(f"[0:a]volume=enable='{expr}':volume=0[am]")
        asrc = "[am]"
    parts.append(f"{asrc}asplit={n}" + "".join(f"[as{i}]" for i in range(n)))
    for i, (s, e) in enumerate(keeps):
        parts.append(f"[vs{i}]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[as{i}]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
    joins = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{joins}concat=n={n}:v=1:a=1[outv][outa]")
    return [*head, "-filter_complex", ";".join(parts), "-map", "[outv]", "-map", "[outa]", *tail]


def build_peaks(
    media: Path,
    start_ms: int,
    end_ms: int,
    pad_s: float = CLIP_PAD_S,
    per_sec: int = 40,
) -> Optional[dict]:
    """Downsampled audio peaks for the window around a finding, for the
    waveform in the timing editor.

    The browser can't decode an MKV to draw a waveform, so the worker decodes
    the ±``pad_s`` window to mono PCM and reduces it to one peak per
    ``1/per_sec`` s (40/s → a peak every 25 ms, matching the editor's nudge
    step). Returns the window's real bounds so the page can map time → x, and
    the finding's own start/end sit inside it.
    """
    import numpy as np

    win_start = max(0.0, start_ms / 1000 - pad_s)
    win_end = end_ms / 1000 + pad_s
    win_dur = max(0.05, win_end - win_start)
    sr = 8000
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{win_start:.3f}", "-i", str(media),
            "-t", f"{win_dur:.3f}", "-map", "0:a:0?", "-ac", "1", "-ar", str(sr),
            "-f", "s16le", "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None

    n_buckets = max(1, int(round(win_dur * per_sec)))
    data = np.frombuffer(proc.stdout, dtype="<i2")
    if data.size < n_buckets:
        peaks = [0.0] * n_buckets
    else:
        bucket = data.size // n_buckets
        d = np.abs(data[: bucket * n_buckets].astype(np.float32)).reshape(n_buckets, bucket)
        peaks = (d.max(axis=1) / 32768.0).round(3).tolist()

    return {
        "winStartMs": int(round(win_start * 1000)),
        "winEndMs": int(round(win_end * 1000)),
        "perSec": per_sec,
        "peaks": peaks,
    }


def build_filmstrip(
    media: Path,
    start_ms: int,
    end_ms: int,
    pad_s: float = CLIP_PAD_S,
    fps: int = 1,
    thumb_w: int = 160,
) -> Optional[bytes]:
    """A single tiled filmstrip JPEG of the ±``pad_s`` window around a finding,
    for the visual timing editor — the frame-preview counterpart to the
    waveform. One ffmpeg call samples ``fps`` frames/s across the window and
    tiles them into one horizontal strip, so the browser makes a single request
    (not one per thumbnail). ``thumb_w`` × ``fps`` = 160 px/s, matching the
    editor's zoom, so a frame lines up with its real time.
    """
    win_start = max(0.0, start_ms / 1000 - pad_s)
    win_end = end_ms / 1000 + pad_s
    win_dur = max(0.5, win_end - win_start)
    cols = max(1, int(round(win_dur * fps)))
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{win_start:.3f}", "-i", str(media),
            "-t", f"{win_dur:.3f}",
            "-vf", f"fps={fps},scale={thumb_w}:-2,tile={cols}x1",
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def grab_thumbnail(media: Path, at_ms: int) -> Optional[bytes]:
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{at_ms / 1000:.3f}", "-i", str(media),
            "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-2",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-",
        ],
        capture_output=True,
    )
    return proc.stdout or None


def media_runtime_ms(media: Path, timeline: Timeline) -> int:
    """The film's length in ms, for the Studio minimap's full-film scale.

    A single ffprobe (no decode), the same source the render path trusts for
    skip keeps. Falls back to just past the last finding if ffprobe can't read
    the container, so the map still spans every marker rather than collapsing.
    """
    try:
        from .shots import media_duration

        seconds = media_duration(media)
        if seconds and seconds > 0:
            return int(round(seconds * 1000))
    except Exception:
        pass
    last = max((s.endMs for s in timeline.segments), default=0)
    return last + 60_000 if last else 60_000


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review — __TITLE__</title>
<style>
:root{
  --bg:#0d1013; --panel:#16191d; --panel2:#1d2126; --line:#2a2f36;
  --ink:#e6edf3; --dim:#9aa5b1; --dim2:#6e7681;
  --pos:#2ea043; --pos-d:#12361f; --neg:#da3633; --neg-d:#3d1517;
  --pick:#3b82f6; --undecided:#c9a227;
  --radius:12px;
  /* Discreet-mode blur: enough to soften the picture without hiding what's
     happening — you still need to see the content to review it. Tune this one value. */
  --discreet-blur:9px;
  /* one colour + one glyph per category, severity-ranked (mirrors _MERGE_SEVERITY) */
  --c-nudity:#ff5c8a; --c-sexual_activity:#ff6b6b; --c-intense_kissing:#ff9f45;
  --c-suggestive:#e0b341; --c-violence:#a78bfa; --c-gore:#e5484d; --c-profanity:#4aa3ff;
  --c-manual:#8b98a5;
  font-family:system-ui,-apple-system,Segoe UI,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
button{font:inherit;cursor:pointer;border:0;border-radius:8px;background:var(--panel2);color:var(--ink);padding:8px 12px}
button:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--pick);outline-offset:2px}
.hidden{display:none!important}
.mono{font-variant-numeric:tabular-nums}
kbd{font-family:ui-monospace,monospace}

.cchip{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
  padding:3px 9px 3px 7px;border-radius:99px;line-height:1;white-space:nowrap}
.cchip .g{font-size:13px}
.tentative-flag{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;
  color:#0d1013;background:var(--undecided);padding:2px 6px;border-radius:5px}

/* Fixed-viewport app shell: the whole studio fits the screen and never scrolls
   the page — only the findings rail and the stage scroll, inside their own
   bounds. (Without min-height:0 on the scrolling children a grid/flex item grows
   to its content instead of scrolling, which is what pushed the page past 100vh
   and produced the extra page scrollbar.) */
#D{display:flex;flex-direction:column;height:100vh;overflow:hidden}
#D .dtop,#D .filmtl,#D .dmerge,#D .khint{flex:0 0 auto}
/* One scrollbar style everywhere — the slim rounded thumb, matching the editor's. */
#D *{scrollbar-width:thin;scrollbar-color:#39424e transparent}
#D *::-webkit-scrollbar{width:12px;height:12px}
#D *::-webkit-scrollbar-track{background:transparent}
#D *::-webkit-scrollbar-thumb{background:#39424e;border:3px solid transparent;background-clip:padding-box;border-radius:99px}
#D *::-webkit-scrollbar-thumb:hover{background:#4a5561;border:3px solid transparent;background-clip:padding-box}

#D .dtop{padding:14px 22px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20;
  background:linear-gradient(#0d1013,#0d1013e6);backdrop-filter:blur(6px)}
#D .dtoprow{display:flex;align-items:center;gap:16px}
#D .dtop h1{margin:0;font-size:17px;font-weight:650}
#D .dtop .path{color:var(--dim2);font-size:12px;margin-top:1px}
/* Top-left library switcher: a combobox to jump to any video in any collection. */
#D .switcher{position:relative}
#D .swtrigger{display:flex;align-items:center;gap:10px;background:transparent;padding:3px 8px 3px 4px;border-radius:9px;text-align:left;max-width:min(58vw,640px)}
#D .swtrigger:hover{background:var(--panel2)}
#D .swtrigger h1{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:50vw}
#D .swtrigger .path{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:50vw}
#D .swcaret{color:var(--dim2);font-size:12px;flex:0 0 auto}
#D .swpanel{position:absolute;top:calc(100% + 6px);left:0;z-index:40;width:min(560px,82vw);background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:0 16px 44px #000b;overflow:hidden}
#D .swinput{width:100%;box-sizing:border-box;background:#0d1117;color:var(--ink);border:0;border-bottom:1px solid var(--line);padding:12px 14px;font:inherit;font-size:14px;outline:none}
#D .swhead{padding:7px 14px;font-size:11px;color:var(--dim2);border-bottom:1px solid var(--line);background:#12161b}
#D .swlist{max-height:min(56vh,460px);overflow-y:auto}
#D .swrow{display:flex;align-items:center;gap:10px;padding:9px 14px;cursor:pointer;border-bottom:1px solid #ffffff08}
#D .swrow:hover,#D .swrow.active{background:var(--panel2)}
#D .swrow .swmeta{flex:1;min-width:0}
#D .swrow .swn{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#D .swrow .swc{font-size:11px;color:var(--dim2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
#D .swrow .swcur-dot{color:var(--pick)}
#D .tt .clock{cursor:text;border-bottom:1px dashed #4a5561;padding-bottom:1px}
#D .tt .clock:hover{border-bottom-color:var(--pick);color:#cfe3ff}
#D .tt input{width:9.5ch;background:#0d1117;color:var(--ink);border:1px solid var(--pick);border-radius:5px;
  padding:1px 4px;font:inherit;font-variant-numeric:tabular-nums;outline:none}
#D .renderbtn{flex:0 0 auto;padding:9px 15px;border-radius:9px;border:1px solid var(--line);
  background:var(--panel2);color:var(--ink);font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap}
#D .renderbtn:hover:not(:disabled){border-color:var(--pick);color:#cfe3ff}
#D .renderbtn:disabled{opacity:.55;cursor:default}
#D .renderbtn.busy{border-color:var(--pick);color:#cfe3ff}
/* The render dialog: two clear targets, never a silent overwrite. */
#D .rov{position:fixed;inset:0;background:#000a;display:flex;align-items:center;justify-content:center;z-index:60}
#D .rdlg{width:min(520px,94vw);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}
#D .rdlg h2{margin:0 0 4px;font-size:16px}
#D .rdlg .rsub{color:var(--dim2);font-size:12.5px;margin-bottom:14px}
#D .rdlg .ropt{display:block;width:100%;text-align:left;padding:12px 14px;margin-bottom:8px;border-radius:10px;
  border:1px solid var(--line);background:var(--panel2);color:var(--ink);font:inherit;cursor:pointer}
#D .rdlg .ropt:hover{border-color:var(--pick)}
#D .rdlg .ropt.on{border-color:var(--pick);background:#1b2836}
#D .rdlg .ropt b{display:block;font-size:13.5px}
#D .rdlg .ropt span{display:block;color:var(--dim2);font-size:11.5px;margin-top:2px;font-family:ui-monospace,monospace}
#D .rdlg .rfoot{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
#D .rdlg .rfoot button{padding:9px 15px;border-radius:9px;border:1px solid var(--line);background:var(--panel2);color:var(--ink);font:inherit;cursor:pointer}
#D .rdlg .rfoot .go{background:var(--pick);border-color:var(--pick);color:#fff;font-weight:600}
#D .rdlg .rmsg{color:var(--dim2);font-size:12px;margin-top:10px;min-height:1em}
#D .swbadge{flex:0 0 auto;font-size:10.5px;font-weight:700;padding:3px 8px;border-radius:99px;letter-spacing:.2px}
#D .swbadge.ready{background:#3a2c12;color:#f0c05a}
#D .swbadge.in_progress{background:#152a3d;color:#7cc0ff}
#D .swbadge.reviewed{background:#12331f;color:#5ee27f}
#D .swbadge.unanalyzed{background:#22262c;color:var(--dim)}
#D .swbadge.corrupt{background:#3a1414;color:#ff6e6e}
#D .swanalyze{flex:0 0 auto;font-size:10.5px;padding:3px 9px;background:#243244;color:#cfe3ff;border-radius:6px}
#D .swanalyze:hover{background:#2d4054}
#D .swanalyze.busy{opacity:.6;pointer-events:none}
#D .swempty{padding:16px 14px;color:var(--dim2);font-size:12.5px;line-height:1.5}
#D .discreet-toggle{margin-left:auto;display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--dim);
  background:var(--panel2);border:1px solid var(--line);border-radius:99px;padding:6px 12px;cursor:pointer;user-select:none}
#D .discreet-toggle.on{background:#2a2140;border-color:#6d4bb0;color:#d8c9ff}
#D .discreet-toggle .sw{width:30px;height:16px;border-radius:99px;background:#3a4048;position:relative;transition:.15s}
#D .discreet-toggle.on .sw{background:#8b5cf6}
#D .discreet-toggle .sw::after{content:'';position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:99px;background:#fff;transition:.15s}
#D .discreet-toggle.on .sw::after{left:16px}
#D .prog{margin-top:11px;display:flex;align-items:center;gap:14px}
#D .progbar{flex:1;height:11px;border-radius:99px;background:#22262c;overflow:hidden;display:flex}
#D .progbar>span{height:100%;transition:width .3s}
#D .progbar .p-cut{background:var(--neg)} #D .progbar .p-leave{background:var(--pos)} #D .progbar .p-un{background:repeating-linear-gradient(45deg,#3a4048 0 7px,#31373e 7px 14px)}
#D .progstats{display:flex;gap:15px;font-size:12px;color:var(--dim)} #D .progstats b{color:var(--ink)}
#D .progstats .dot{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:6px;vertical-align:1px}

#D .filmtl{padding:10px 22px 6px}
#D .filmtl .ftrack{position:relative;height:30px;background:#171b20;border-radius:7px;cursor:pointer;overflow:hidden}
#D .filmtl .ftick{position:absolute;top:0;bottom:0;width:3px;transform:translateX(-1px);border-radius:2px;opacity:.9;z-index:1}
#D .filmtl .fbox{position:absolute;top:0;bottom:0;background:rgba(88,166,255,.16);border:1px solid #58a6ff;
  box-shadow:0 0 0 1px #0006 inset;border-radius:4px;cursor:grab;z-index:2;min-width:6px}
#D .filmtl .fbox:active{cursor:grabbing}
#D .filmtl .fph{position:absolute;top:-2px;bottom:-2px;width:2px;background:#fff;box-shadow:0 0 5px #000;z-index:3}
#D .filmtl .fph::before{content:'';position:absolute;top:-1px;left:-4px;border:5px solid transparent;border-top-color:#fff}
#D .filmtl .flbl{display:flex;justify-content:space-between;font-size:10.5px;color:var(--dim2);margin-top:4px}

#D .dwork{display:grid;grid-template-columns:340px 1fr;gap:0;flex:1 1 auto;min-height:0}
#D .drail{border-right:1px solid var(--line);overflow-y:auto;min-height:0;padding:8px}
#D .drailhead{display:flex;align-items:center;justify-content:space-between;padding:4px 8px 8px;font-size:11px;color:var(--dim2);text-transform:uppercase;letter-spacing:.5px}
#D .drailhead button{font-size:11px;padding:4px 9px;background:var(--panel2);color:var(--dim);text-transform:none;letter-spacing:0}
#D .drailhead button.on{background:#243244;color:#cfe3ff}
#D .drow{padding:9px 10px;border-radius:9px;cursor:pointer;border:1px solid transparent;border-left:3px solid transparent;margin-bottom:2px}
#D .drow:hover{background:var(--panel)}
#D .drow.cur{background:var(--panel2);border-color:#33404e}
#D .drow.cut{border-left-color:var(--neg)} #D .drow.leave{border-left-color:var(--pos);opacity:.72}
#D .drow .r1{display:flex;align-items:center;gap:8px}
#D .drow .cglyph{font-size:12px}
#D .drow .cname{font-size:12.5px;font-weight:650;text-transform:capitalize}
#D .drow .rtime{margin-left:auto;font-size:11px;color:var(--dim2);font-variant-numeric:tabular-nums}
#D .drow .rdesc{font-size:11.5px;color:var(--dim);margin-top:3px;line-height:1.4}
#D .drow .rdesc .word{color:#ffd479;font-weight:600}
#D .drow .r3{display:flex;gap:6px;margin-top:7px;align-items:center}
#D .drow .qd{font-size:11px;padding:4px 9px;border-radius:6px;background:var(--panel2);color:var(--dim);font-weight:600;flex:1}
#D .drow .qd.cut.on{background:var(--neg);color:#fff} #D .drow .qd.leave.on{background:var(--pos);color:#04220d}
#D .drow .mpick{width:16px;height:16px;border-radius:5px;border:1.5px solid #40474f;display:grid;place-items:center;font-size:10px;font-weight:800;color:transparent}
#D .drow .mpick.on{background:var(--pick);border-color:var(--pick);color:#fff}
#D .drailempty{padding:18px 10px;color:var(--dim2);font-size:12.5px;line-height:1.5}

#D .dstage{overflow-y:auto;min-height:0;padding:16px 20px}
#D .monitor{position:relative;aspect-ratio:16/9;max-height:34vh;margin:0 auto;border-radius:12px;overflow:hidden;background:#000;display:grid;place-items:center}
/* the <video> is always present (so audio keeps playing even in Visual mode,
   where the frame image covers it); display is controlled inline in D_monitor. */
#D .monitor .mvideo{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000;z-index:0}
#D .monitor .mframe{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000;z-index:1}
#D .monitor .grad{position:absolute;inset:0;z-index:1}
/* Discreet = the picture (frame OR moving video) is heavily blurred but still
   shown, so you can play through a bad scene to find/edit it without seeing the
   detail. Hold-to-reveal drops the blur for a clean peek. */
#D .monitor.discreet .mframe,#D .monitor.discreet .grad,#D .monitor.discreet .mvideo{filter:blur(var(--discreet-blur)) brightness(.9) saturate(.95);transform:scale(1.06)}
#D .monitor .mnote{position:relative;z-index:3;text-align:center;color:#fff;text-shadow:0 2px 10px #000;pointer-events:none;padding:0 12px}
#D .monitor .mnote .glyph{font-size:34px}
#D .monitor .mnote .lab{margin-top:6px;font-size:13px;font-weight:600}
#D .monitor .mnote .sub{font-size:11.5px;color:#d0d6dd;margin-top:2px}
/* No longer a full cover — just a small corner badge saying privacy is on, so
   the blurred picture you navigate by stays visible underneath. */
#D .monitor .veil{position:absolute;top:10px;left:12px;z-index:4;display:flex;align-items:center;gap:6px;background:#0b0d10cc;color:#d8c9ff;font-size:11px;letter-spacing:.3px;padding:4px 9px;border-radius:7px;border:1px solid #6d4bb055;pointer-events:none}
#D .monitor .reveal{position:absolute;bottom:10px;right:10px;z-index:4;font-size:11px;background:#000a;color:#fff;border:1px solid #fff4;padding:5px 10px;border-radius:7px}
#D .monitor .scrub{position:absolute;top:10px;left:12px;z-index:4;font-size:11px;color:#9be7c8;background:#0008;padding:3px 9px;border-radius:6px;display:none}
#D .monitor.scrubbing .scrub{display:block}
#D .monitor .cliploading{position:absolute;inset:0;z-index:6;display:none;align-items:center;justify-content:center;gap:9px;
  background:#0b0d10cc;color:#cfe3ff;font-size:12.5px;letter-spacing:.2px}
#D .monitor.loadingclip .cliploading{display:flex}
#D .monitor .cliploading .spin{width:15px;height:15px;border-radius:99px;border:2px solid #3b6ea5;border-top-color:transparent;animation:D-spin .8s linear infinite}
@keyframes D-spin{to{transform:rotate(360deg)}}
/* Generic inline spinner for anything else waiting on a fetch (the switcher
   list, the render plan, …) — currentColor so it fits wherever it's dropped. */
#D .spin{display:inline-block;width:11px;height:11px;border-radius:99px;border:2px solid currentColor;
  border-top-color:transparent;opacity:.7;animation:D-spin .8s linear infinite;vertical-align:-2px;margin-right:6px}
#D .transport{display:flex;align-items:center;gap:12px;margin:10px auto 0;max-width:640px}
#D .transport button{background:var(--panel2)}
#D .transport .pp{background:#fff;color:#000;font-weight:700;width:42px;height:42px;border-radius:99px;font-size:16px}
#D .transport .tt{font-variant-numeric:tabular-nums;font-size:13px;color:var(--dim)}
#D .transport .tt b{color:var(--ink)}
#D .transport .now{margin-left:auto;font-size:12px;color:var(--dim2)}
#D .playopts{display:flex;align-items:center;justify-content:center;gap:14px;margin:9px auto 0;max-width:640px;flex-wrap:wrap}
#D .segbtn{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel2)}
#D .segbtn .lbl{font-size:11px;color:var(--dim2);padding:0 9px}
#D .segbtn button{border-radius:0;background:transparent;color:var(--dim);font-size:12px;padding:7px 11px;font-weight:600}
#D .segbtn button.on{background:#243244;color:#cfe3ff}

/* rail: type filter + bulk */
#D .dtypebar{display:flex;flex-wrap:wrap;gap:5px;padding:2px 8px 8px}
#D .dtypebar button{font-size:11px;padding:4px 9px;background:var(--panel2);color:var(--dim);border-radius:99px;font-weight:600;display:inline-flex;align-items:center;gap:5px;text-transform:capitalize}
#D .dtypebar button.on{background:#243244;color:#cfe3ff;box-shadow:inset 0 0 0 1px #3b6ea5}
#D .dtypebar button .n{opacity:.6;font-weight:500}
#D .dbulk{display:flex;align-items:center;gap:7px;padding:8px;margin:0 4px 6px;background:#151b24;border:1px solid #24344a;border-radius:9px;font-size:12px}
#D .dbulk .grow{flex:1}
#D .dbulk #D-bulklabel{color:var(--dim)}
#D .dbulk button{font-size:11.5px;padding:6px 10px;font-weight:600}
#D .dbulk .bcut{background:var(--neg);color:#fff} #D .dbulk .bleave{background:var(--pos);color:#04220d}

#D .edcard{margin-top:10px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
#D .edhead{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
#D .edhead .cchip{cursor:default}
#D .edhead .zoomhint{margin-left:auto;font-size:11px;color:var(--dim2)}
#D .edscroll{overflow:hidden;padding-bottom:2px}
/* a horizontal scrollbar for the zoomed editor: the track is the whole film,
   the thumb is the viewport — drag it to pan without going up to the minimap. */
#D .edbarrow{display:flex;align-items:center;gap:6px;margin:6px 0 2px}
#D .edstep{flex:0 0 auto;width:28px;height:15px;padding:0;display:grid;place-items:center;font-size:10px;line-height:1;
  background:#1b2027;border:1px solid var(--line);border-radius:6px;color:var(--dim);user-select:none}
#D .edstep:hover{background:#243244;color:#cfe3ff}
#D .edstep:active{background:var(--pick);color:#fff}
#D .edbar{position:relative;flex:1;height:15px;border-radius:8px;background:#0c0f13;border:1px solid var(--line);cursor:pointer}
#D .edbar .edthumb{position:absolute;top:1px;bottom:1px;min-width:16px;border-radius:7px;background:#39424e;border:1px solid #4a5561;cursor:grab}
#D .edbar .edthumb:hover{background:#455060}
#D .edbar .edthumb:active{cursor:grabbing;background:var(--pick)}
#D .edtrack{position:relative;user-select:none}
#D .edfilm{position:relative;height:52px;border-radius:6px 6px 0 0;overflow:hidden;cursor:crosshair;background:#0c0c0d}
#D .edfilm .edfilmimg{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;display:block}
#D .edfilm .shotmark{position:absolute;top:0;bottom:0;width:2px;background:#ffffff66;pointer-events:none;z-index:2}
#D .edwave{display:block;background:#0c0c0d;cursor:crosshair}
#D .wavespin{position:absolute;top:4px;right:6px;width:13px;height:13px;border-radius:99px;
  border:2px solid #6b7580;border-top-color:transparent;animation:D-spin .8s linear infinite;pointer-events:none}
#D .edlane{position:relative;height:52px;margin-top:6px;background:#14181d;border-radius:6px;overflow:hidden;cursor:crosshair}
#D .edlane .keeplane{position:absolute;inset:0;display:grid;place-items:center;color:#3f6d4e;font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;pointer-events:none}
#D .region{position:absolute;top:4px;bottom:4px;border-radius:6px;cursor:grab;overflow:hidden;border:1px solid;box-shadow:0 2px 6px #0006}
#D .region.sel{outline:2px solid #fff;z-index:5}
#D .region.leave{opacity:.38;filter:grayscale(.4)}
#D .region .rlabel{position:absolute;top:4px;left:7px;font-size:10px;font-weight:800;color:#fff;text-shadow:0 1px 2px #000;pointer-events:none;letter-spacing:.3px}
#D .region .rlen{position:absolute;bottom:3px;left:7px;font-size:9.5px;color:#fffd;text-shadow:0 1px 2px #000;pointer-events:none}
#D .region .rotag{position:absolute;top:4px;right:7px;font-size:9px;font-weight:700;color:#ffdcae;text-shadow:0 1px 2px #000;pointer-events:none}
#D .region .redge{position:absolute;top:0;bottom:0;width:9px;cursor:ew-resize;z-index:4}
#D .region .redge.l{left:0} #D .region .redge.r{right:0}
#D .edph{position:absolute;top:0;bottom:0;width:2px;background:#fff;box-shadow:0 0 5px #000;z-index:8;pointer-events:none}
#D .edph.buffering{animation:D-phbuffer .9s ease-in-out infinite}
@keyframes D-phbuffer{0%,100%{opacity:1}50%{opacity:.35}}
#D .edsel{position:absolute;top:0;bottom:0;background:rgba(88,166,255,.22);border-left:2px solid #58a6ff;border-right:2px solid #58a6ff;z-index:6;pointer-events:none;display:none}
#D .edtools{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-top:12px}
#D .edtools button{font-size:12px}
#D .edtools .add{background:var(--pos);color:#04220d;font-weight:650} #D .edtools .split{background:#3b6ea5;color:#fff;font-weight:600}
#D .edtools kbd{font-size:10.5px;opacity:.7;margin-left:3px}
#D .edtools .sep{width:1px;height:22px;background:var(--line);margin:0 3px}
#D .edtools .note{margin-left:auto;font-size:11.5px;color:#ffb877;min-height:15px}
#D .edform{margin-top:12px;display:grid;grid-template-columns:auto 1fr;gap:9px 12px;align-items:center;font-size:12.5px}
#D .edform label{color:var(--dim2);font-size:11.5px}
#D .edform .val{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
#D .edform input,#D .edform select,#D .edform textarea{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px;font:inherit;font-size:12px}
#D .edform input.time{width:120px;font-variant-numeric:tabular-nums}
#D .edform textarea{width:100%;min-height:38px;resize:vertical}
#D .edform .nudge{padding:5px 8px;background:var(--panel2);font-size:11px;font-variant-numeric:tabular-nums}
#D .edform .dur{color:var(--dim2);font-size:11.5px}
#D .edform .ro{color:#ffb877;font-size:11px}
#D .eddecide{display:flex;gap:9px;margin-top:14px;align-items:center;flex-wrap:wrap}
#D .eddecide .big{flex:1;min-width:130px;padding:13px;font-size:14px;font-weight:650;border-radius:9px;border:1.5px solid var(--line);background:var(--panel2);display:flex;align-items:center;justify-content:center;gap:8px}
#D .eddecide .big.cut.on{background:var(--neg);border-color:var(--neg);color:#fff}
#D .eddecide .big.leave.on{background:var(--pos);border-color:var(--pos);color:#04220d}
#D .eddecide .trash{background:transparent;color:var(--dim2);border:1px solid var(--line);font-size:12px}
#D .edresult{margin-top:12px;font-size:12px;color:var(--dim);line-height:1.6}
#D .edresult .keep{color:#5ee27f;font-weight:600} #D .edresult .warn{color:#ffb877;font-weight:600}

#D .dmerge{padding:9px 22px;background:#151b24;border-bottom:1px solid #24344a;display:flex;gap:10px;align-items:center;font-size:12.5px}
#D .dmerge b{color:#cfe3ff} #D .dmerge .grow{flex:1}
#D .dmerge button.go{background:#3b6ea5;color:#fff;font-weight:600} #D .dmerge button.go:disabled{opacity:.4;cursor:default}
#D .dmerge button.x{background:transparent;color:var(--dim)}
#D .khint{color:var(--dim2);font-size:11.5px;padding:8px 22px;border-top:1px solid var(--line);display:flex;gap:16px;flex-wrap:wrap}
#D .khint kbd{background:var(--panel2);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 6px;font-family:inherit;font-size:10.5px;color:var(--ink)}

/* Touch: these surfaces are dragged with a finger (scrub, pan, resize a region),
   so stop the browser's own scroll/zoom/callout from competing with the JS drag
   (wired as Pointer Events, which unify mouse + touch + pen in one code path). */
#D .edbar,#D .edthumb,#D .ftrack,#D .fbox,#D .region,#D .edfilm,#D .edwave,#D .edlane,
#D .reveal,#D .edstep{touch-action:none;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
#D .edzoom{display:flex;gap:5px;flex:0 0 auto}

/* Mobile: the app shell is desktop-first (a fixed 340px findings rail beside the
   editor); below this width there isn't room for both, so switch to one full-
   width pane at a time with a small tab bar to flip between them. Selecting a
   finding jumps to the Player tab automatically (see D_select/D_mtabSet). */
#D .mtabbar{display:none;gap:8px;padding:8px 22px 0}
#D .mtabbar button{flex:1;padding:11px;font-weight:700;font-size:13px;border-radius:9px;background:var(--panel2);color:var(--dim)}
#D .mtabbar button.on{background:#243244;color:#cfe3ff}
@media (max-width:900px){
  #D .mtabbar{display:flex}
  #D .dwork{display:flex;flex-direction:column}
  #D .drail{display:none;flex:1 1 auto;min-height:0;border-right:none}
  #D .dstage{display:none;flex:1 1 auto;min-height:0;padding:14px 16px}
  #D .drail.mshow,#D .dstage.mshow{display:block}
  #D .khint{display:none}
  #D .dtoprow{flex-wrap:wrap;row-gap:8px}
  #D .swtrigger{max-width:100%}
  #D .swtrigger h1,#D .swtrigger .path{max-width:70vw}
  /* bigger touch targets — the desktop sizes above are mouse-tuned */
  #D .drow .qd{padding:11px 12px;font-size:13px}
  #D .drow{padding:12px 11px}
  #D .transport{gap:16px}
  #D .transport button{min-height:42px;padding:10px 14px}
  #D .transport .pp{width:54px;height:54px;font-size:18px}
  #D .segbtn button{padding:10px 13px;font-size:13px}
  #D .edstep{width:36px;height:34px}
  #D .eddecide .big{padding:16px;font-size:15px}
  #D .reveal{padding:9px 14px;font-size:12.5px}
  /* The left/right edge-drag handles that retime a finding's start/end are 9px
     wide (fine for a mouse pointer, too thin for a fingertip). 22px is a
     compromise, not the usual 44px minimum — a region can be quite narrow at a
     wide zoom, and two 44px handles would swallow the whole body, leaving no
     room to drag the region as a whole (D_dragBody). Zooming in first (the new
     +/- buttons) widens the region and makes the handle easier to land on too. */
  #D .region .redge{width:22px}
}
</style>

<section id="D">
  <div class="dtop">
    <div class="dtoprow">
      <div class="switcher" id="D-switcher">
        <button class="swtrigger" id="D-swtrigger" title="Switch to another video — search any collection (press /)">
          <div class="swcur">
            <h1>__TITLE__</h1>
            <div class="path mono">__PATH_DISPLAY__</div>
          </div>
          <span class="swcaret">▾</span>
        </button>
        <div class="swpanel hidden" id="D-swpanel">
          <input class="swinput" id="D-swinput" placeholder="Search any video in any collection…" autocomplete="off" spellcheck="false">
          <div class="swhead" id="D-swhead"></div>
          <div class="swlist" id="D-swlist"></div>
        </div>
      </div>
      <button class="renderbtn" id="D-render" title="Write a clean copy from the findings you approved">🎬 Render clean copy</button>
      <div class="discreet-toggle on" id="D-discreet" title="Blur the picture so you can review without others seeing the content">
        <span class="sw"></span> Discreet mode
      </div>
    </div>
    <div class="prog">
      <div class="progbar" id="D-progbar"></div>
      <div class="progstats" id="D-progstats"></div>
    </div>
  </div>
  <div class="filmtl">
    <div class="ftrack" id="D-ftrack"><div class="fbox" id="D-fbox" title="What the editor below is showing — drag to pan, zoom the editor to resize"></div><div class="fph" id="D-fph"></div></div>
    <div class="flbl"><span>0:00</span><span>click to jump · drag the box to pan the editor · markers are findings</span><span id="D-runtime">0:00</span></div>
  </div>
  <div class="mtabbar" id="D-mtabbar">
    <button class="on" id="D-mtab-stage">▶ Player</button>
    <button id="D-mtab-findings">☰ Findings <span id="D-mtab-count"></span></button>
  </div>
  <div class="dmerge hidden" id="D-mergebar">
    <b><span id="D-mergecount">0</span> picked</b>
    <span id="D-mergehint" style="color:var(--dim2)"></span>
    <div class="grow"></div>
    <button class="go" id="D-mergego" disabled>Merge into one</button>
    <button class="x" id="D-mergeclear">Clear</button>
  </div>
  <div class="dwork">
    <aside class="drail">
      <div class="drailhead"><span>Findings</span>
        <button id="D-mergemode">⇄ Merge…</button></div>
      <div class="dtypebar" id="D-typechips"></div>
      <div class="dbulk hidden" id="D-bulkrow">
        <span id="D-bulklabel"></span>
        <div class="grow"></div>
        <button class="bcut" id="D-bulkcut">✂ Cut all</button>
        <button class="bleave" id="D-bulkleave">👁 Leave all</button>
      </div>
      <div id="D-list"></div>
    </aside>
    <main class="dstage">
      <div class="monitor discreet" id="D-monitor">
        <video class="mvideo" id="D-clip" playsinline preload="metadata"></video>
        <img class="mframe" id="D-mframe" alt="">
        <div class="grad" id="D-mgrad"></div>
        <div class="veil" id="D-veil">🔒 blurred · discreet</div>
        <div class="mnote" id="D-mnote"></div>
        <div class="scrub">♪ playing audio for this scene</div>
        <div class="cliploading" id="D-cliploading"><span class="spin"></span> building clip… (transcoding this scene)</div>
        <button class="reveal" id="D-reveal">👁 Hold to reveal</button>
      </div>
      <div class="transport">
        <button class="pp" id="D-pp">▶</button>
        <button id="D-back1">◀ 1s</button>
        <button id="D-fwd1">1s ▶</button>
        <span class="tt mono" id="D-tt"></span>
        <span class="now" id="D-now"></span>
      </div>
      <div class="playopts">
        <div class="segbtn" id="D-picmode" title="Show frames only (private) or play the real video">
          <button data-pic="visual" class="on">🖼 Visual</button>
          <button data-pic="video">🎬 Video</button>
        </div>
        <div class="segbtn" id="D-audmode" title="How the audio plays back">
          <span class="lbl">Audio</span>
          <button data-aud="normal" class="on">Normal</button>
          <button data-aud="cleaned" title="Apply this finding's decision: skips jumped, mutes/voice silenced — hear it as the viewer will">Cleaned</button>
          <button data-aud="muted">Muted</button>
        </div>
        <div class="segbtn" id="D-range" title="Play just this scene (fast, cached) or stream the whole film from the playhead">
          <span class="lbl">Range</span>
          <button data-range="scene" class="on">Scene</button>
          <button data-range="film" title="Hold play and the film runs continuously across scenes from the playhead">Film</button>
        </div>
      </div>
      <div class="edcard" id="D-edcard"></div>
    </main>
  </div>
  <div class="khint">
    <span><kbd>J</kbd> <kbd>K</kbd> findings</span>
    <span><kbd>Space</kbd> play/pause</span>
    <span><kbd>A</kbd> add cut</span>
    <span><kbd>S</kbd> split</span>
    <span><kbd>Del</kbd> delete region</span>
    <span><kbd>C</kbd> cut out</span>
    <span><kbd>L</kbd> leave in</span>
    <span><kbd>Shift</kbd>+drag = audition · wheel = zoom</span>
  </div>
</section>

<script>
// ---------- injected by the worker ----------
const MEDIA = __MEDIA_JSON__;
const PAD = __PAD__;
const RUNTIME_MS = __RUNTIME_MS__;
let SEGS = __SEGS_JSON__;

// ---------- category system (mirrors worker/policy.py severity order) ----------
const CAT = {
  nudity:{g:'●',sev:5}, sexual_activity:{g:'●',sev:4}, intense_kissing:{g:'♥',sev:4},
  suggestive:{g:'◐',sev:2}, violence:{g:'⚔',sev:3}, gore:{g:'✦',sev:5},
  profanity:{g:'“”',sev:1}, manual:{g:'✎',sev:0},
};
const CATEGORIES=['profanity','suggestive','intense_kissing','sexual_activity','nudity','violence','gore','manual'];
const VISUAL_CATS=['nudity','sexual_activity','intense_kissing','suggestive','violence','gore'];
function isVisual(s){return s.engine==='vlm'||s.engine==='pureframe'||VISUAL_CATS.includes(s.category);}
const catColor = c => getComputedStyle(document.documentElement).getPropertyValue('--c-'+c).trim() || '#8b98a5';
const TENTATIVE=['suggestive'];

// action → region look (skip works live; mute/voice/blur are render-only)
const C_ACT={
  skip:{c:'#f85149',bg:'repeating-linear-gradient(45deg,#f8514977 0 8px,#f8514922 8px 16px)',lbl:'SKIP'},
  mute:{c:'#4aa3ff',bg:'#4aa3ff44',lbl:'MUTE'},
  voice:{c:'#2dd4bf',bg:'#2dd4bf44',lbl:'VOICE'},
  blur:{c:'#a78bfa',bg:'#a78bfa44',lbl:'BLUR'}};
const actLabels={mute:'Mute (silence all)',voice:'Voice-only mute',blur:'Blur',skip:'Skip'};
const engineName={subtitle:'subtitle text',subtitles:'subtitle text',whisper:'speech (Whisper)',
  vlm:'vision AI',pureframe:'frame heuristics',manual:'added by hand'};

// ---------- helpers ----------
const pad=(n,l=2)=>String(n).padStart(l,'0');
const fmtHMS=ms=>{ms=Math.max(0,Math.floor(ms));const t=Math.floor(ms/1000);
  return `${Math.floor(t/3600)}:${pad(Math.floor(t%3600/60))}:${pad(t%60)}.${pad(Math.floor(ms%1000),3)}`;};
const fmtShort=ms=>{const t=Math.floor(Math.max(0,ms)/1000);return `${Math.floor(t/60)}:${pad(t%60)}`;};
const word=s=>{const m=/^\[([^\]]+)\]/.exec(s.reasoning||'');return m?m[1]:null;};
const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const friendly=s=>{
  const w=word(s);
  if(w) return `Muted word <span class="word">“${esc(w)}”</span> in the dialogue.`;
  return esc((s.reasoning||'').replace(/;?\s*policy:.*$/,'').trim())||'—';
};
function gradFor(s){const c=s?catColor(s.category):'#8b98a5';
  return `radial-gradient(120% 120% at 30% 20%, ${c}44, #000 60%), linear-gradient(135deg, ${c}22, #000)`;}
function cchip(s){const c=catColor(s.category);const t=TENTATIVE.includes(s.category);
  return `<span class="cchip" style="background:${c}22;color:${c}">
    <span class="g">${CAT[s.category]?.g||'●'}</span>${esc(s.category.replace(/_/g,' '))}</span>`
    + (t?` <span class="tentative-flag">needs your call</span>`:'');}
// Flexible parse for a typed time: H:MM:SS.mmm, MM:SS(.mmm), or plain seconds.
function parseTime(str){
  str=String(str==null?'':str).trim();
  if(!str)return null;
  const p=str.split(':').map(x=>parseFloat(x));
  if(!p.length||p.some(x=>!isFinite(x)))return null;
  let sec;
  if(p.length===3)sec=p[0]*3600+p[1]*60+p[2];
  else if(p.length===2)sec=p[0]*60+p[1];
  else sec=p[0];
  return Math.max(0,Math.round(sec*1000));
}
function C_merge(iv){const s=iv.map(x=>x.slice()).sort((a,b)=>a[0]-b[0]);const o=[];
  for(const v of s){if(o.length&&v[0]<=o[o.length-1][1])o[o.length-1][1]=Math.max(o[o.length-1][1],v[1]);else o.push(v.slice());}return o;}

// ---------- persistence (every decision reaches the sidecar) ----------
function patchSeg(id,body){
  return fetch(`/api/segments/${id}?path=${encodeURIComponent(MEDIA)}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}
function createSeg(body){
  return fetch(`/api/segments?path=${encodeURIComponent(MEDIA)}`,{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>{if(!r.ok)throw new Error('create failed');return r.json();});
}
function deleteSeg(id){
  return fetch(`/api/segments/${id}?path=${encodeURIComponent(MEDIA)}`,{method:'DELETE'})
    .then(r=>{if(!r.ok)throw new Error('delete failed');return r.json();});
}
function mergeSeg(ids,action){
  return fetch(`/api/segments/merge?path=${encodeURIComponent(MEDIA)}`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids,recommendedAction:action,approved:true})})
    .then(r=>{if(!r.ok)throw new Error('merge failed');return r.json();});
}
// Re-read the whole timeline (all findings, not approvedOnly) after a structural
// change, so ids and shape match the sidecar exactly.
function refetch(){
  return fetch(`/api/segments?path=${encodeURIComponent(MEDIA)}&approvedOnly=false`)
    .then(r=>r.json()).then(tl=>{SEGS=tl.segments||[];});
}

// ---------- state ----------
const D_PAD=15000;
let D={sel:SEGS[0]?SEGS[0].id:null, playMs:SEGS[0]?SEGS[0].startMs:0, discreet:true, playing:false, timer:null,
       mtab:'stage',                 // mobile only: 'stage' (player/editor) | 'findings' (the list)
       viewStart:null, viewEnd:null, merge:false, picks:new Set(), scrubbing:false, held:false,
       peaksKey:null, peaks:null, frameKey:null, frameTimer:null,
       typeFilter:'all',            // scope the whole workspace to one finding type
       picMode:'visual',            // 'visual' (frames+audio) | 'video' (real picture+audio)
       audioMode:'normal',          // 'normal' | 'cleaned' (acted-on) | 'muted'
       range:'scene',               // 'scene' (per-window clip) | 'film' (whole-film stream)
       clipStartAbs:0, cutStart:null, cutEnd:null,
       clipKey:null, loadingClip:false,     // dedup the transcoded clip + show a build state
       keeps:null, playWinStart:0,           // cleaned playback: clip-time → film-time map
       sa:{ctx:null,buf:null,key:null,winStart:0,winEnd:0,loading:false,last:0}};  // live scrub-audio (WebAudio grains)

// The reviewer-facing "type" of a finding: a profane word is its own group,
// everything else its category — matches the old page's type filter.
function typeOf(s){return word(s)||s.category;}
function D_visible(s){return D.typeFilter==='all'||typeOf(s)===D.typeFilter;}
function D_shown(){return SEGS.filter(D_visible);}

function D_get(id){return SEGS.find(s=>s.id===id);}
function D_nearest(ms){
  if(!SEGS.length)return null;
  const inside=SEGS.filter(s=>ms>=s.startMs&&ms<=s.endMs).sort((a,b)=>(a.endMs-a.startMs)-(b.endMs-b.startMs))[0];
  if(inside)return inside;
  let best=null,bd=Infinity;
  SEGS.forEach(s=>{const d=Math.min(Math.abs(ms-s.startMs),Math.abs(ms-s.endMs));if(d<bd){bd=d;best=s;}});
  return best;
}
// approved===true => CUT OUT (red); ===false => LEAVE IN (green); null => undecided
function D_state(s){return s.approved===true?'cut':s.approved===false?'leave':'undecided';}

// ---------- viewport / minimap ----------
function D_edW(){const c=document.getElementById('D-edcard');return Math.max(320,(c?c.clientWidth:900)-28);}
function D_ex(ms){const span=(D.viewEnd-D.viewStart)||1;return (ms-D.viewStart)/span*D_edW();}
function D_clampView(){let span=D.viewEnd-D.viewStart;
  if(span>=RUNTIME_MS){D.viewStart=0;D.viewEnd=RUNTIME_MS;return;}
  if(D.viewStart<0){D.viewEnd-=D.viewStart;D.viewStart=0;}
  if(D.viewEnd>RUNTIME_MS){D.viewStart-=(D.viewEnd-RUNTIME_MS);D.viewEnd=RUNTIME_MS;if(D.viewStart<0)D.viewStart=0;}}
// Zoom the editor viewport by `factor` (<1 = in, >1 = out) around `centerT` (ms).
// Shared by the wheel handler (desktop) and the +/- buttons (no wheel on a phone).
function D_zoomBy(factor,centerT){
  const span=D.viewEnd-D.viewStart||1;
  const newSpan=Math.max(3000,Math.min(RUNTIME_MS,span*factor));
  const frac=(centerT-D.viewStart)/span;
  D.viewStart=centerT-frac*newSpan;D.viewEnd=D.viewStart+newSpan;D_clampView();
  D_editor();D_filmtl();
}
function D_ensureView(){if(D.viewStart==null){const s=D_get(D.sel)||SEGS[0];
  if(!s){D.viewStart=0;D.viewEnd=Math.min(RUNTIME_MS,60000);return;}
  const span=Math.min(RUNTIME_MS,Math.max(24000,(s.endMs-s.startMs)+30000));
  const c=(s.startMs+s.endMs)/2;D.viewStart=c-span/2;D.viewEnd=c+span/2;D_clampView();}}
function D_centerView(s){const span=(D.viewEnd-D.viewStart)||40000;const c=(s.startMs+s.endMs)/2;
  D.viewStart=c-span/2;D.viewEnd=c+span/2;D_clampView();}
function D_highlightNearest(){const n=D_nearest(D.playMs);if(!n)return;
  document.querySelectorAll('#D-list .drow').forEach(r=>r.classList.toggle('cur',+r.dataset.id===n.id));}

// ---------- render ----------
function D_render(){
  // A filter that hides the selected finding would leave the editor on
  // something off-screen — snap the selection into the shown set.
  if(D.sel!=null){const cur=D_get(D.sel);if(cur&&!D_visible(cur)){const first=D_shown()[0];D.sel=first?first.id:null;}}
  D_ensureView();
  D_prog(); D_typechips(); D_bulkrow(); D_filmtl(); D_list(); D_monitor(); D_editor(); D_mergebar();
  document.getElementById('D-runtime').textContent=fmtShort(RUNTIME_MS);
  const mc=document.getElementById('D-mtab-count');if(mc)mc.textContent=SEGS.length?`(${SEGS.length})`:'';
}

// One chip per type present, plus All; picking one scopes the whole workspace.
function D_typechips(){
  const row=document.getElementById('D-typechips');
  const counts={};SEGS.forEach(s=>{const t=typeOf(s);counts[t]=(counts[t]||0)+1;});
  const order=Object.keys(counts).sort((a,b)=>counts[b]-counts[a]||a.localeCompare(b));
  const chip=(key,label,n)=>`<button data-type="${esc(key)}" class="${key===D.typeFilter?'on':''}">${esc(label)}<span class="n">${n}</span></button>`;
  row.innerHTML=chip('all','All',SEGS.length)+order.map(t=>chip(t,t.replace(/_/g,' '),counts[t])).join('');
  row.querySelectorAll('button').forEach(b=>b.onclick=()=>{D.typeFilter=b.dataset.type;D_render();});
}

// Bulk cut/leave acts on exactly the findings the current filter shows.
function D_bulkrow(){
  const bar=document.getElementById('D-bulkrow');
  const shown=D_shown();
  const on=D.typeFilter!=='all';
  bar.classList.toggle('hidden',!on);
  if(!on)return;
  document.getElementById('D-bulklabel').innerHTML=`<b>${shown.length}</b> ${esc(D.typeFilter.replace(/_/g,' '))} shown`;
}
function D_bulk(v){
  const ids=D_shown().map(s=>s.id);
  if(!ids.length)return;
  fetch(`/api/segments?path=${encodeURIComponent(MEDIA)}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,approved:v})})
    .then(r=>r.json()).then(tl=>{const by={};(tl.segments||[]).forEach(x=>by[x.id]=x);
      SEGS.forEach(s=>{if(by[s.id])s.approved=by[s.id].approved;});D_render();})
    .catch(()=>D_ednote&&D_ednote('bulk failed'));
}

function D_prog(){
  const n=SEGS.length||1;
  const c=SEGS.filter(s=>s.approved===true).length, l=SEGS.filter(s=>s.approved===false).length, u=SEGS.length-c-l;
  const p=x=>100*x/n;
  document.getElementById('D-progbar').innerHTML=
    `<span class="p-cut" style="width:${p(c)}%"></span><span class="p-leave" style="width:${p(l)}%"></span><span class="p-un" style="width:${p(u)}%"></span>`;
  document.getElementById('D-progstats').innerHTML=
    `<span><span class="dot" style="background:var(--neg)"></span><b>${c}</b> cut out</span>`+
    `<span><span class="dot" style="background:var(--pos)"></span><b>${l}</b> left in</span>`+
    `<span><span class="dot" style="background:#3a4048"></span><b>${u}</b> to review</span>`;
}

function D_filmtl(){
  const track=document.getElementById('D-ftrack'), box=document.getElementById('D-fbox');
  [...track.querySelectorAll('.ftick')].forEach(n=>n.remove());
  D_shown().forEach(s=>{const mid=(s.startMs+s.endMs)/2;const t=document.createElement('div');
    t.className='ftick';t.style.left=100*mid/RUNTIME_MS+'%';t.style.background=catColor(s.category);
    if(s.id===D.sel)t.style.outline='2px solid #fff';track.insertBefore(t,box);});
  box.style.left=100*D.viewStart/RUNTIME_MS+'%';
  box.style.width=100*(D.viewEnd-D.viewStart)/RUNTIME_MS+'%';
  document.getElementById('D-fph').style.left=100*D.playMs/RUNTIME_MS+'%';
}

function D_list(){
  const list=document.getElementById('D-list');
  if(!SEGS.length){list.innerHTML='<div class="drailempty">No findings for this film yet. Scrub the film and press <b>A</b> to add a cut where you spot something.</div>';return;}
  const shown=D_shown();
  if(!shown.length){list.innerHTML='<div class="drailempty">No findings of this type. Clear the filter above to see the rest.</div>';return;}
  list.innerHTML=shown.map(s=>{
    const st=D_state(s), w=word(s);
    return `<div class="drow ${s.id===D.sel?'cur':''} ${st}" data-id="${s.id}">
      <div class="r1">
        ${D.merge?`<div class="mpick ${D.picks.has(s.id)?'on':''}" data-pick="${s.id}">✓</div>`:''}
        <span class="cglyph" style="color:${catColor(s.category)}">${CAT[s.category]?.g||'●'}</span>
        <span class="cname">${w?`“${esc(w)}”`:esc(s.category.replace(/_/g,' '))}</span>
        <span class="rtime">${fmtShort(s.startMs)}</span>
      </div>
      <div class="rdesc">${friendly(s)}</div>
      <div class="r3">
        <button class="qd cut ${st==='cut'?'on':''}" data-qd="cut" data-id="${s.id}">✂ Cut out</button>
        <button class="qd leave ${st==='leave'?'on':''}" data-qd="leave" data-id="${s.id}">👁 Leave in</button>
      </div>
    </div>`;
  }).join('');
  list.querySelectorAll('.drow').forEach(el=>{
    const id=+el.dataset.id;
    el.addEventListener('click',e=>{
      if(e.target.closest('[data-qd]')||e.target.closest('[data-pick]'))return;
      D_select(id);
    });
  });
  list.querySelectorAll('[data-qd]').forEach(b=>b.onclick=e=>{e.stopPropagation();
    const s=D_get(+b.dataset.id), want=b.dataset.qd==='cut';
    D_setApproved(s,(D_state(s)===b.dataset.qd)?null:want);});
  list.querySelectorAll('[data-pick]').forEach(b=>b.onclick=e=>{e.stopPropagation();
    const id=+b.dataset.pick; D.picks.has(id)?D.picks.delete(id):D.picks.add(id); D_render();});
}

// Mobile tab switch (Player <-> Findings) — CSS gates whether this has any
// visible effect (both panes show side by side above the breakpoint), so this
// is safe to call unconditionally.
function D_mtabSet(tab){
  D.mtab=tab;
  const stageOn=tab==='stage';
  document.getElementById('D-mtab-stage').classList.toggle('on',stageOn);
  document.getElementById('D-mtab-findings').classList.toggle('on',!stageOn);
  document.querySelector('#D .drail').classList.toggle('mshow',!stageOn);
  document.querySelector('#D .dstage').classList.toggle('mshow',stageOn);
}
function D_select(id){D.sel=id;const s=D_get(id);if(s){D.playMs=s.startMs;D_centerView(s);}D_mtabSet('stage');D_render();}

// ---------- monitor (real frame at the playhead via the thumbnail endpoint) ----------
function D_loadFrame(){
  const img=document.getElementById('D-mframe');if(!img)return;
  const key=Math.round(D.playMs/500)*500;
  if(key===D.frameKey)return;
  D.frameKey=key;
  img.src=`/api/thumbnail?path=${encodeURIComponent(MEDIA)}&ms=${Math.max(0,key)}`;
}
function D_scheduleFrame(){
  if(D.frameTimer)clearTimeout(D.frameTimer);
  D.frameTimer=setTimeout(D_loadFrame,160);
}
function D_monitor(){
  const s=D_nearest(D.playMs), mon=document.getElementById('D-monitor');
  const videomode=D.picMode==='video';
  mon.classList.toggle('videomode',videomode);
  // Discreet blurs the picture (frame OR moving video) but keeps it visible, so
  // you can play through a bad scene to find and edit it without seeing detail.
  // Hold-to-reveal (D.held) drops the blur for a clean peek. Picture mode just
  // chooses frame vs moving video — blur applies to either.
  const blur=D.discreet&&!D.held;
  mon.classList.toggle('discreet',blur);
  document.getElementById('D-veil').style.display=blur?'flex':'none';
  document.getElementById('D-mgrad').style.display='none';   // real (blurred) picture shows; no colour stand-in
  // The moving <video> is the picture once playing in Video mode; otherwise the
  // frame image carries Visual playback + scrubbing. Both are blurred by the
  // .discreet class when blur is on.
  const showVideoPicture=videomode&&D.playing;
  const img=document.getElementById('D-mframe');
  img.style.display=showVideoPicture?'none':'block';
  if(!showVideoPicture)D_scheduleFrame();
  const inside=s&&D.playMs>=s.startMs&&D.playMs<=s.endMs;
  document.getElementById('D-mnote').innerHTML='';   // picture is always visible now; badge + transport carry context
  mon.classList.toggle('scrubbing',D.scrubbing);
  mon.classList.toggle('loadingclip',D.loadingClip);
  // The clock is an entry point, not just a readout: click it and type a time
  // to land on an exact moment, which dragging the playhead cannot do. Left
  // alone while it is being typed in, or every redraw would wipe the entry.
  const tt=document.getElementById('D-tt');
  if(!tt.querySelector('input'))
    tt.innerHTML=`<b class="clock" title="Click to type an exact time">${fmtHMS(D.playMs)}</b> / ${fmtShort(RUNTIME_MS)}`;
  document.getElementById('D-now').textContent = inside
    ? `now: #${s.id} ${s.category.replace(/_/g,' ')}` : 'now: clear';
  document.getElementById('D-pp').textContent=D.playing?'❚❚':'▶';
}

// ---------- editor ----------
const HEAVY_MS=180000;  // beyond this window, skip the ffmpeg strip/peaks fetch
function D_editor(){
  // Render the scrollable timeline even with no finding selected (incl. zero findings):
  // the whole point of the empty state is to scrub and press A to add the first cut.
  const s=D_get(D.sel);
  const vs=D.viewStart, ve=D.viewEnd, span=Math.max(1,ve-vs), W=D_edW();
  const ex=ms=>(ms-vs)/span*W;
  const regions=SEGS.filter(r=>r.endMs>vs&&r.startMs<ve&&D_visible(r));
  const acts=s?['skip','mute','voice','blur'].map(a=>`<option value="${a}" ${s.recommendedAction===a?'selected':''}>${actLabels[a]}</option>`).join(''):'';
  const cats=s?CATEGORIES.map(c=>`<option value="${c}" ${s.category===c?'selected':''}>${c.replace(/_/g,' ')}</option>`).join(''):'';
  const st=s?D_state(s):'';
  const heavy=span>HEAVY_MS;
  // filmstrip: a single tiled JPEG for the viewport window (any scene), stacked over the waveform.
  const strip = heavy
    ? '<div style="position:absolute;inset:0;display:grid;place-items:center;color:var(--dim2);font-size:11px">zoom in to load frames</div>'
    : `<img class="edfilmimg" src="/api/filmstrip?path=${encodeURIComponent(MEDIA)}&startMs=${Math.round(vs)}&endMs=${Math.round(ve)}&pad=0" alt="">`;
  // shot marks: the boundaries of the analysed (visual) findings inside the view.
  let shots='';
  SEGS.filter(r=>isVisual(r)&&r.startMs>vs&&r.startMs<ve).forEach(r=>{
    shots+=`<div class="shotmark" style="left:${ex(r.startMs)}px"></div>`;});

  document.getElementById('D-edcard').innerHTML=`
    <div class="edhead">${s?cchip(s)+`<span class="mono" style="color:var(--dim2);font-size:12px">#${s.id} · detected by ${esc(engineName[s.engine]||s.engine)}</span>`:'<span class="mono" style="color:var(--dim2);font-size:12px">No finding selected — scrub, then press <b>A</b> to add a cut</span>'}
      <span class="zoomhint">scroll or +/− = zoom · drag the box on the map to pan · showing ${fmtShort(vs)}–${fmtShort(ve)}</span>
      <div class="edzoom"><button class="edstep" id="D-zoomout" title="Zoom out">−</button><button class="edstep" id="D-zoomin" title="Zoom in">+</button></div></div>
    <div class="edscroll" id="D-edscroll">
      <div class="edtrack" style="width:${W}px" id="D-edtrack">
        <div class="edfilm" style="width:${W}px">${strip}${shots}</div>
        <canvas class="edwave" width="${W}" height="46" style="width:${W}px"></canvas>
        <div class="wavespin" id="D-wavespin" hidden></div>
        <div class="edlane" style="width:${W}px" id="D-edlane"><div class="keeplane">plays normally where no region covers</div></div>
        <div class="edsel" id="D-edsel"></div>
        <div class="edph" id="D-edph"></div>
      </div>
    </div>
    <div class="edbarrow">
      <button class="edstep" id="D-edleft" title="Scrub a little left (hold to repeat)">◀</button>
      <div class="edbar" id="D-edbar" title="Drag to pan the film · click to jump"><div class="edthumb" id="D-edthumb"></div></div>
      <button class="edstep" id="D-edright" title="Scrub a little right (hold to repeat)">▶</button>
    </div>
    <div class="edtools">
      <button class="add" id="D-add">＋ Cut at playhead<kbd>A</kbd></button>
      <button class="split" id="D-split">⁄ Split<kbd>S</kbd></button>
      <button id="D-delregion">✕ Delete region<kbd>Del</kbd></button>
      <span class="note" id="D-ednote"></span>
    </div>
    ${s?`<div class="edform" id="D-edform">
      <label>Start</label><div class="val">
        <input class="time" id="D-start" value="${fmtHMS(s.startMs)}">
        <button class="nudge" data-edit="start" data-d="-1000">◀1s</button>
        <button class="nudge" data-edit="start" data-d="-25">◀25ms</button>
        <button class="nudge" data-edit="start" data-d="25">25ms▶</button>
        <button class="nudge" data-edit="start" data-d="1000">1s▶</button></div>
      <label>End</label><div class="val">
        <input class="time" id="D-end" value="${fmtHMS(s.endMs)}">
        <button class="nudge" data-edit="end" data-d="-1000">◀1s</button>
        <button class="nudge" data-edit="end" data-d="-25">◀25ms</button>
        <button class="nudge" data-edit="end" data-d="25">25ms▶</button>
        <button class="nudge" data-edit="end" data-d="1000">1s▶</button>
        <span class="dur" id="D-dur">${((s.endMs-s.startMs)/1000).toFixed(2)}s</span></div>
      <label>Category</label><div class="val"><select id="D-cat">${cats}</select>
        <label style="margin-left:6px">Action</label><select id="D-act">${acts}</select>
        ${s.recommendedAction!=='skip'?'<span class="ro">render-only</span>':''}</div>
      <label>Description</label><div class="val"><textarea id="D-desc">${esc(s.reasoning||'')}</textarea></div>
    </div>
    <div class="eddecide">
      <button class="big cut ${st==='cut'?'on':''}" id="D-cut">✂ Cut it out <kbd>C</kbd></button>
      <button class="big leave ${st==='leave'?'on':''}" id="D-leave">👁 Leave it in <kbd>L</kbd></button>
      <button class="trash" id="D-trash">🗑 Delete finding</button>
    </div>`:''}
    <div class="edresult" id="D-edresult"></div>`;

  // waveform — real peaks for the viewport, cached by window so only a real
  // pan/zoom refetches (decisions and edits redraw from the cache).
  D_drawWave(W);

  // regions (clipped to the viewport edges so a finding wider than the view still shows)
  const lane=document.getElementById('D-edlane');
  regions.forEach(r=>{const a=C_ACT[r.recommendedAction]||C_ACT.skip;
    const L=Math.max(0,ex(r.startMs)), R=Math.min(W,ex(r.endMs));
    const el=document.createElement('div');el.className='region'+(r.id===D.sel?' sel':'')+(r.approved===false?' leave':'');
    el.style.cssText=`left:${L}px;width:${Math.max(4,R-L)}px;background:${a.bg};border-color:${a.c}`;
    el.innerHTML=`<div class="redge l"></div><div class="redge r"></div><div class="rlabel">${a.lbl}</div>`
      +`<div class="rlen mono">${((r.endMs-r.startMs)/1000).toFixed(1)}s</div>`
      +(r.recommendedAction!=='skip'?'<div class="rotag">render-only</div>':'');
    el.addEventListener('pointerdown',e=>{
      if(e.target.classList.contains('redge'))D_dragEdge(e,r,e.target.classList.contains('l'),el);
      else D_dragBody(e,r,el);});
    lane.appendChild(el);});
  const edph=document.getElementById('D-edph');
  if(D.playMs>=vs&&D.playMs<=ve){edph.style.display='block';edph.style.left=ex(D.playMs)+'px';}else edph.style.display='none';

  // horizontal scrollbar: thumb = viewport over the whole film; drag to pan,
  // click the track to jump. Lets you move without reaching for the minimap.
  const edbar=document.getElementById('D-edbar'), edthumb=document.getElementById('D-edthumb');
  const placeThumb=()=>{const L=Math.max(0,Math.min(1,D.viewStart/RUNTIME_MS));
    const wf=Math.max(0.02,Math.min(1,(D.viewEnd-D.viewStart)/RUNTIME_MS));
    edthumb.style.left=(L*100)+'%'; edthumb.style.width=(wf*100)+'%';};
  placeThumb();
  edthumb.addEventListener('pointerdown',e=>{e.preventDefault();e.stopPropagation();
    const rect=edbar.getBoundingClientRect(), sp=D.viewEnd-D.viewStart, sx=e.clientX, vs0=D.viewStart;
    const move=ev=>{D.viewStart=vs0+(ev.clientX-sx)/rect.width*RUNTIME_MS;D.viewEnd=D.viewStart+sp;D_clampView();D_filmtl();D_editor();};
    const up=()=>{document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);D_render();};
    document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);});
  edbar.addEventListener('pointerdown',e=>{if(e.target===edthumb)return;
    const rect=edbar.getBoundingClientRect(), sp=D.viewEnd-D.viewStart;
    const c=(e.clientX-rect.left)/rect.width*RUNTIME_MS;   // center the viewport where you clicked
    D.viewStart=c-sp/2;D.viewEnd=D.viewStart+sp;D_clampView();D_filmtl();D_editor();});
  // ◀/▶ step buttons: nudge the playhead a little bit; hold to repeat (fine scrub)
  const holdStep=(id,dir)=>{const b=document.getElementById(id);if(!b)return;
    b.addEventListener('pointerdown',e=>{e.preventDefault();if(D.playing)D_stop();
      D.scrubbing=true; D_saCtx(); D_scrubStep(dir);
      const iv=setInterval(()=>D_scrubStep(dir),120);
      const stop=()=>{clearInterval(iv);D.scrubbing=false;D_saStop();
        document.removeEventListener('pointerup',stop);document.removeEventListener('pointerleave',stop);document.removeEventListener('pointercancel',stop);
        const n=D_nearest(D.playMs);if(n)D.sel=n.id;D_render();};
      document.addEventListener('pointerup',stop);document.addEventListener('pointerleave',stop);document.addEventListener('pointercancel',stop);});};
  holdStep('D-edleft',-1); holdStep('D-edright',1);

  // wheel = zoom the viewport around the cursor (the minimap box shrinks/grows to match)
  const track=document.getElementById('D-edtrack');
  document.getElementById('D-edscroll').addEventListener('wheel',e=>{e.preventDefault();
    const cursorT=vs+(e.clientX-track.getBoundingClientRect().left)/W*span;
    D_zoomBy(e.deltaY<0?0.82:1.22,cursorT);},{passive:false});
  // +/- buttons: the touch equivalent of the wheel above (no wheel on a phone).
  // Centered on the playhead when it's in view, else the view's midpoint.
  const zoomCenter=()=>(D.playMs>=vs&&D.playMs<=ve)?D.playMs:(vs+ve)/2;
  document.getElementById('D-zoomin').onclick=()=>D_zoomBy(0.72,zoomCenter());
  document.getElementById('D-zoomout').onclick=()=>D_zoomBy(1.4,zoomCenter());

  // scrub (plain drag on film/wave/lane bg); shift+drag = audition
  const cv=document.querySelector('#D-edcard canvas');
  const tOf=cx=>Math.max(vs,Math.min(ve,Math.round((vs+(cx-track.getBoundingClientRect().left)/W*span)/25)*25));
  [document.querySelector('#D-edcard .edfilm'),cv,lane].forEach(bg=>{ if(!bg)return;
    bg.addEventListener('pointerdown',e=>{
      if(e.target.closest('.region'))return; e.preventDefault();
      if(e.shiftKey){D_audition(e);return;}
      if(D.playing)D_stop();   // seeking wins over playback — else ontimeupdate fights the scrub
      D.scrubbing=true; D_saCtx();   // prime the audio context on this gesture
      const move=ev=>{D.playMs=tOf(ev.clientX);edph.style.display='block';edph.style.left=ex(D.playMs)+'px';D_scrubAudio(D.playMs);D_monitor();D_filmtl();D_highlightNearest();};
      move(e);
      const up=()=>{D.scrubbing=false;D_saStop();document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);
        const n=D_nearest(D.playMs);if(n)D.sel=n.id;D_render();};
      document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);
    });
  });

  document.getElementById('D-add').onclick=()=>D_add();
  document.getElementById('D-split').onclick=D_split;
  document.getElementById('D-delregion').onclick=D_delRegion;
  if(s){
    document.getElementById('D-cut').onclick=()=>D_decide(s,true);
    document.getElementById('D-leave').onclick=()=>D_decide(s,false);
    document.getElementById('D-trash').onclick=()=>{if(confirm('Delete finding #'+s.id+'?'))D_deleteFinding(s);};
    document.getElementById('D-cat').onchange=e=>{s.category=e.target.value;patchSeg(s.id,{category:s.category});D_render();};
    document.getElementById('D-act').onchange=e=>{s.recommendedAction=e.target.value;patchSeg(s.id,{recommendedAction:s.recommendedAction});D_render();};
    document.getElementById('D-desc').onchange=e=>{s.reasoning=e.target.value;patchSeg(s.id,{reasoning:s.reasoning});D_render();};
    document.getElementById('D-start').onchange=e=>{const v=parseTime(e.target.value);if(v!=null){s.startMs=v;D_fixOrder(s);D_saveTiming(s);D_render();}};
    document.getElementById('D-end').onchange=e=>{const v=parseTime(e.target.value);if(v!=null){s.endMs=v;D_fixOrder(s);D_saveTiming(s);D_render();}};
    document.querySelectorAll('#D-edcard .nudge').forEach(b=>b.onclick=()=>{
      const d=+b.dataset.d;if(b.dataset.edit==='start')s.startMs=Math.max(0,s.startMs+d);else s.endMs=Math.max(0,s.endMs+d);
      D_fixOrder(s);D_saveTiming(s);D_render();});
  }
  D_edresult(s,D.viewStart,D.viewEnd);
}
function D_fixOrder(s){if(s.endMs<s.startMs){const t=s.startMs;s.startMs=s.endMs;s.endMs=t;}}
function D_saveTiming(s){patchSeg(s.id,{startMs:s.startMs,endMs:s.endMs});}

function D_drawWave(W){
  const cv=document.querySelector('#D-edcard canvas');if(!cv)return;
  const vs=D.viewStart, ve=D.viewEnd, span=Math.max(1,ve-vs);
  const draw=peaks=>{
    const ctx=cv.getContext('2d');ctx.clearRect(0,0,W,46);ctx.fillStyle='#3a4650';
    if(!peaks||!peaks.length)return;
    const n=peaks.length, bw=W/n;
    for(let i=0;i<n;i++){const h=Math.max(2,peaks[i]*42);ctx.fillRect(i*bw,(46-h)/2,Math.max(1,bw-0.4),h);}
  };
  const key=Math.round(vs)+'-'+Math.round(ve);
  if(key===D.peaksKey){draw(D.peaks);return;}
  draw(null);
  if(span>HEAVY_MS)return;
  const tok=key;D.peaksKey=key;
  const spin=document.getElementById('D-wavespin');
  if(spin)spin.hidden=false;
  fetch(`/api/peaks?path=${encodeURIComponent(MEDIA)}&startMs=${Math.round(vs)}&endMs=${Math.round(ve)}&pad=0`)
    .then(r=>r.json()).then(d=>{
      if(D.peaksKey!==tok)return;   // a newer window won the race
      if(spin)spin.hidden=true;
      D.peaks=d.peaks||[];
      const cv2=document.querySelector('#D-edcard canvas');
      if(cv2){const w2=cv2.width;const ctx=cv2.getContext('2d');ctx.clearRect(0,0,w2,46);ctx.fillStyle='#3a4650';
        const n=D.peaks.length,bw=w2/(n||1);
        for(let i=0;i<n;i++){const h=Math.max(2,D.peaks[i]*42);ctx.fillRect(i*bw,(46-h)/2,Math.max(1,bw-0.4),h);}}
    }).catch(()=>{if(D.peaksKey===tok){D.peaksKey=null;if(spin)spin.hidden=true;}});
}

function D_edresult(s,winStart,winEnd){
  const el=document.getElementById('D-edresult');if(!el)return;
  const inWin=SEGS.filter(r=>r.endMs>winStart&&r.startMs<winEnd&&(r.approved===true));
  const cov=C_merge(inWin.map(r=>[r.startMs,r.endMs]));
  const cut=cov.reduce((a,[x,y])=>a+(y-x),0);
  el.innerHTML=`In this ${((winEnd-winStart)/1000)|0}s window: <b style="color:var(--ink)">${inWin.length}</b> region(s) set to cut, `
    +`removing <span class="warn">${(cut/1000).toFixed(1)}s</span>. Gaps between them play normally — split a region and delete the middle to keep a beat.`;
}

// ---------- carve ops (real create / patch / delete against the sidecar) ----------
function D_ednote(m){const n=document.getElementById('D-ednote');if(!n)return;n.textContent=m;setTimeout(()=>{if(n)n.textContent='';},1700);}
function D_add(){
  const st=D.playMs,en=Math.min(st+1500,D.viewEnd);
  if(en-st<200){D_ednote('playhead at edge');return;}
  const src=D_get(D.sel);
  D_ednote('adding…');
  createSeg({startMs:Math.round(st),endMs:Math.round(en),category:src?src.category:'manual',
    recommendedAction:'skip',approved:true,reasoning:'added by hand'})
    .then(seg=>refetch().then(()=>{D.sel=seg.id;D_ednote('');D_render();}))
    .catch(()=>D_ednote('could not add'));
}
function D_split(){
  const r=SEGS.find(x=>D.playMs>x.startMs+100&&D.playMs<x.endMs-100);
  if(!r){D_ednote('put the playhead inside a region to split');return;}
  const cut=Math.round(D.playMs), origEnd=r.endMs;
  D_ednote('splitting…');
  // Second half becomes a new finding carrying the original's category/action/decision;
  // the original is shortened to the playhead. Two findings, gap-free — delete the
  // middle piece later to keep a beat.
  createSeg({startMs:cut,endMs:origEnd,category:r.category,recommendedAction:r.recommendedAction,
    approved:r.approved,reasoning:r.reasoning||''})
    .then(()=>patchSeg(r.id,{endMs:cut}))
    .then(()=>refetch().then(()=>{const keep=D_get(r.id);if(keep)D.sel=keep.id;D_ednote('');D_render();}))
    .catch(()=>D_ednote('could not split'));
}
function D_delRegion(){
  const r=D_get(D.sel);if(!r){D_ednote('select a region');return;}
  D_deleteFinding(r);
}
function D_deleteFinding(r){
  D_ednote('deleting…');
  deleteSeg(r.id).then(()=>refetch().then(()=>{const n=D_nearest(D.playMs);D.sel=n?n.id:null;D_ednote('');D_render();}))
    .catch(()=>D_ednote('could not delete'));
}

// Retiming a region is a scrub: move the playhead to the edge you're dragging and
// sound + show it (grains + frame), so you can place the bound by ear and eye —
// scrubbing with the segment landing right where you want.
function D_dragScrub(ms){
  D.playMs=Math.max(0,Math.min(RUNTIME_MS,ms));
  const edph=document.getElementById('D-edph');
  if(edph){edph.style.display='block';edph.style.left=D_ex(D.playMs)+'px';}
  D_scrubAudio(D.playMs); D_monitor(); D_filmtl();
}
// Nudge the playhead a little bit (the ◀/▶ on the scrollbar) — a fine scrub with
// audio + frame, panning the view to keep the playhead in sight.
const SCRUB_STEP=250;
function D_scrubStep(dir){
  D.playMs=Math.max(0,Math.min(RUNTIME_MS,D.playMs+dir*SCRUB_STEP));
  const span=D.viewEnd-D.viewStart, m=span*0.08;
  let panned=false;
  if(D.playMs<D.viewStart+m){D.viewStart=D.playMs-m;D.viewEnd=D.viewStart+span;D_clampView();panned=true;}
  else if(D.playMs>D.viewEnd-m){D.viewEnd=D.playMs+m;D.viewStart=D.viewEnd-span;D_clampView();panned=true;}
  if(panned)D_editor();       // redraw the strip for the new window
  D_dragScrub(D.playMs);      // playhead + grain + frame (no editor rebuild if not panned)
}
function D_dragBody(e,r,el){e.preventDefault();if(D.playing)D_stop();D.sel=r.id;
  D.scrubbing=true; D_saCtx();
  const W=D_edW(),span=D.viewEnd-D.viewStart;const sx=e.clientX,s0=r.startMs,dur=r.endMs-r.startMs;
  const move=ev=>{let ns=Math.round((s0+(ev.clientX-sx)/W*span)/25)*25;ns=Math.max(0,ns);r.startMs=ns;r.endMs=ns+dur;
    el.style.left=D_ex(r.startMs)+'px';D_dragScrub(r.startMs);};   // follow the leading edge
  const up=()=>{D.scrubbing=false;D_saStop();document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);
    D_saveTiming(r);SEGS.sort((a,b)=>a.startMs-b.startMs);D_render();};
  document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);}
function D_dragEdge(e,r,isLeft,el){e.preventDefault();if(D.playing)D_stop();D.sel=r.id;
  D.scrubbing=true; D_saCtx();
  const W=D_edW(),span=D.viewEnd-D.viewStart,track=document.getElementById('D-edtrack');
  // Capture the track's left ONCE. Calling D_render() mid-drag (as this used to)
  // rebuilds the editor and detaches this track element, so a fresh
  // getBoundingClientRect() on the stale node returns 0 and the edge leaps to the
  // right on the next move. Instead: resize the element in place, D_render on up.
  const rectLeft=track.getBoundingClientRect().left;
  const move=ev=>{const x=ev.clientX-rectLeft;let t=Math.round((D.viewStart+x/W*span)/25)*25;
    if(isLeft)r.startMs=Math.min(Math.max(0,t),r.endMs-200);else r.endMs=Math.max(t,r.startMs+200);
    if(el){el.style.left=D_ex(r.startMs)+'px';el.style.width=Math.max(4,D_ex(r.endMs)-D_ex(r.startMs))+'px';
      const rl=el.querySelector('.rlen');if(rl)rl.textContent=((r.endMs-r.startMs)/1000).toFixed(1)+'s';}
    D_dragScrub(isLeft?r.startMs:r.endMs);};   // follow the edge under the cursor
  const up=()=>{D.scrubbing=false;D_saStop();document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);D_saveTiming(r);D_render();};
  document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);}

function D_audition(e){const sel=document.getElementById('D-edsel'),track=document.getElementById('D-edtrack');
  const W=D_edW(),span=D.viewEnd-D.viewStart,ox=track.getBoundingClientRect().left;
  const tOf=cx=>D.viewStart+(cx-ox)/W*span;
  let a=tOf(e.clientX),b=a;
  const draw=()=>{const lo=Math.min(a,b),hi=Math.max(a,b);sel.style.display='block';
    sel.style.left=D_ex(lo)+'px';sel.style.width=Math.max(0,D_ex(hi)-D_ex(lo))+'px';};
  draw();
  const move=ev=>{b=tOf(ev.clientX);draw();};
  const up=()=>{document.removeEventListener('mousemove',move);document.removeEventListener('mouseup',up);
    const lo=Math.min(a,b),hi=Math.max(a,b);
    if(hi-lo<120){sel.style.display='none';return;}   // a stray click, not a drag
    D_ednote('▶ auditioning '+fmtShort(lo)+'–'+fmtShort(hi));
    D_auditionPlay(lo,hi,()=>{sel.style.display='none';});};
  document.addEventListener('mousemove',move);document.addEventListener('mouseup',up);}

// Play just the selected span through the shared clip element (real audio),
// so a reviewer can hear what's there before moving a handle.
function D_auditionPlay(lo,hi,done){
  const v=document.getElementById('D-clip');
  const clipStart=Math.max(0,lo/1000-0.25);
  v.onloadedmetadata=v.ontimeupdate=v.onplay=v.onpause=v.onended=v.onerror=null;
  v.src=`/api/clip?path=${encodeURIComponent(MEDIA)}&startMs=${Math.round(lo)}&endMs=${Math.round(hi)}&pad=0.25`;
  v.muted=D.audioMode==='muted';
  v.onloadedmetadata=()=>{v.currentTime=Math.max(0,lo/1000-clipStart);v.play().catch(()=>{});};
  v.ontimeupdate=()=>{D.playMs=(clipStart+v.currentTime)*1000;D.scrubbing=true;D_monitor();D_filmtl();D_highlightNearest();};
  const end=()=>{D.scrubbing=false;D_monitor();if(done)done();};
  v.onended=end;v.onerror=()=>{D_ednote&&D_ednote('could not load clip');end();};
  v.load();}

// ---------- decisions ----------
function D_setApproved(s,v){s.approved=v;patchSeg(s.id,{approved:v});D_render();}
function D_decide(s,cut){D_setApproved(s,(D_state(s)===(cut?'cut':'leave'))?null:cut);}

// ---------- merge ----------
function D_mergebar(){
  const bar=document.getElementById('D-mergebar');
  document.getElementById('D-mergemode').classList.toggle('on',D.merge);
  bar.classList.toggle('hidden',!D.merge);
  if(!D.merge)return;
  const picked=[...D.picks].map(D_get).filter(Boolean);
  const cats=new Set(picked.map(p=>p.category));
  document.getElementById('D-mergecount').textContent=picked.length;
  const ok=picked.length>=2&&cats.size===1;
  document.getElementById('D-mergehint').textContent=
    picked.length<2?'tick 2+ findings of the same type':cats.size>1?'pick findings of ONE type to merge':'ready — '+[...cats][0].replace(/_/g,' ');
  document.getElementById('D-mergego').disabled=!ok;
}
function D_doMerge(){
  const picked=[...D.picks].map(D_get).filter(Boolean);
  const cats=new Set(picked.map(p=>p.category));
  if(picked.length<2||cats.size!==1)return;
  const ids=picked.map(p=>p.id);
  const btn=document.getElementById('D-mergego');
  btn.disabled=true;const label=btn.textContent;btn.textContent='Merging…';
  mergeSeg(ids,'skip').then(()=>refetch().then(()=>{
    D.picks.clear();D.merge=false;const n=D_nearest(D.playMs);D.sel=n?n.id:null;D_render();}))
    .catch(()=>{D_ednote('could not merge');btn.disabled=false;btn.textContent=label;});
}

// ---------- playback (Phase 1: per-window clip, with real audio) ----------
// The clip endpoint returns a seekable transcoded MP4 *with audio* for the
// selected finding's window (±PAD). Audio mode maps onto the endpoint flags;
// picture mode decides whether the moving video or the frame image is on screen.
// (Whole-film continuous playback is Phase 2 — the streaming endpoint.)
// A tight lead/tail for playback: on a real (MPEG-2) film the clip is transcoded
// on demand, and the pad is dead weight to encode — 3 s is enough run-up to hear
// a word's onset without making a 2-hour source's clip take 15 s to build.
const PLAY_PAD=3;
// The findings a Cleaned preview applies over the selected finding's window:
// every finding you've CUT OUT (approved) plus the one you're working on, so you
// hear the window as the viewer will — all the cuts, not just the highlighted one.
function D_cleanedPlan(s){
  const winStart=Math.max(0,Math.round(s.startMs-PLAY_PAD*1000)), winEnd=Math.round(s.endMs+PLAY_PAD*1000);
  const apply=SEGS.filter(x=>(x.approved===true||x.id===s.id)&&x.endMs>winStart&&x.startMs<winEnd);
  const span=x=>[Math.max(winStart,Math.round(x.startMs)),Math.min(winEnd,Math.round(x.endMs))];
  const cuts=apply.filter(x=>x.recommendedAction==='skip').map(span);
  const mutes=apply.filter(x=>x.recommendedAction==='mute').map(span);
  // Voice-only mutes get real Demucs vocal removal in the cached scene clip
  // (kept separate from hard mutes), so the reviewer hears music through.
  const voices=apply.filter(x=>x.recommendedAction==='voice').map(span);
  const blurs=apply.filter(x=>x.recommendedAction==='blur').map(span);
  return {winStart,winEnd,cuts,mutes,blurs,voices};
}
// clip-time → film-time for a cleaned clip: the skips are gone, so time is
// compressed; walk the kept spans to map where in the film a clip position is.
function D_buildKeeps(winStart,winEnd,cuts){
  const dur=winEnd-winStart;
  const rel=cuts.map(([a,b])=>[Math.max(0,a-winStart),Math.min(dur,b-winStart)]).sort((x,y)=>x[0]-y[0]);
  const merged=[];rel.forEach(([a,b])=>{if(merged.length&&a<=merged[merged.length-1][1])merged[merged.length-1][1]=Math.max(merged[merged.length-1][1],b);else merged.push([a,b]);});
  const keeps=[];let prev=0;merged.forEach(([a,b])=>{if(a>prev)keeps.push([prev,a]);prev=Math.max(prev,b);});if(prev<dur)keeps.push([prev,dur]);
  let acc=0;return keeps.map(([a,b])=>{const k={rel:a,clip:acc,len:b-a};acc+=b-a;return k;});
}
function D_clipToFilm(keeps,winStart,clipMs){
  if(!keeps||!keeps.length)return winStart+clipMs;
  for(const k of keeps){if(clipMs<=k.clip+k.len+1)return winStart+k.rel+Math.max(0,clipMs-k.clip);}
  const last=keeps[keeps.length-1];return winStart+last.rel+last.len;
}
function D_clipSrc(s){
  if(D.audioMode==='cleaned'){
    // Cleaned → a windowed render (skips cut out, mutes silenced) via the
    // preview endpoint, so the skipped footage is never transcoded.
    const p=D_cleanedPlan(s);
    const enc=spans=>spans.map(([a,b])=>a+'-'+b).join(',');
    return `/api/preview_clip?path=${encodeURIComponent(MEDIA)}&startMs=${p.winStart}&endMs=${p.winEnd}`
      +`&cut=${enc(p.cuts)}&mute=${enc(p.mutes)}&blur=${enc(p.blurs)}&voice=${enc(p.voices)}`;
  }
  // Normal / Muted → the plain window clip (audio as-is; Muted mutes the element).
  return `/api/clip?path=${encodeURIComponent(MEDIA)}&startMs=${Math.round(s.startMs)}&endMs=${Math.round(s.endMs)}&pad=${PLAY_PAD}`;
}
function D_playFollow(){
  const v=document.getElementById('D-clip');
  D.playMs=D.keeps?D_clipToFilm(D.keeps,D.playWinStart,v.currentTime*1000):D.clipStartAbs+v.currentTime*1000;
  const n=D_nearest(D.playMs);if(n)D.sel=n.id;
  let panned=false;
  if(D.playMs>D.viewEnd||D.playMs<D.viewStart){const span=D.viewEnd-D.viewStart;
    D.viewStart=D.playMs-span*0.2;D.viewEnd=D.viewStart+span;D_clampView();panned=true;}
  D_monitor();D_filmtl();D_highlightNearest();
  if(panned)D_editor();
  else{const ph=document.getElementById('D-edph');if(ph){
    if(D.playMs>=D.viewStart&&D.playMs<=D.viewEnd){ph.style.display='block';ph.style.left=D_ex(D.playMs)+'px';}else ph.style.display='none';}}
}
// ---------- live scrub audio (WebAudio grains) ----------
// Dragging the playhead pauses the <video>, so there's no stream audio while you
// scrub. To let a reviewer FIND an edit point by ear, we decode a compact WAV of
// a window around the cursor once, then play a short faded grain at the drag
// position on every move — continuous "scrub" sound, DAW-style, at any playhead.
const SA_CAP=90000;        // window we buffer for scrubbing (ms) — ±45s of the cursor
const SA_GRAIN=0.2;        // max grain length (s); the next grain usually stops it first
function D_saCtx(){
  const sa=D.sa;
  if(!sa.ctx){const AC=window.AudioContext||window.webkitAudioContext;if(!AC)return null;sa.ctx=new AC();}
  if(sa.ctx.state==='suspended')sa.ctx.resume();   // must be called from a gesture (scrub mousedown)
  return sa.ctx;
}
function D_saLoad(winStart,winEnd){
  winStart=Math.max(0,Math.round(winStart)); winEnd=Math.min(RUNTIME_MS,Math.round(winEnd));
  if(winEnd-winStart<1000)return;
  const key=winStart+'-'+winEnd;
  const sa=D.sa;
  if(sa.key===key&&(sa.buf||sa.loading))return;    // already have (or fetching) this window
  const ctx=D_saCtx(); if(!ctx)return;
  sa.key=key; sa.buf=null; sa.loading=true; sa.winStart=winStart; sa.winEnd=winEnd;
  // A drag with no scrub sound yet reads as broken, not loading — pulse the
  // playhead while its window decodes so it's clearly "still fetching."
  const ph=document.getElementById('D-edph'); if(ph)ph.classList.add('buffering');
  fetch(`/api/scrub_audio?path=${encodeURIComponent(MEDIA)}&startMs=${winStart}&endMs=${winEnd}`)
    .then(r=>r.ok?r.arrayBuffer():Promise.reject())
    .then(ab=>ctx.decodeAudioData(ab))
    .then(buf=>{if(D.sa.key===key){D.sa.buf=buf;D.sa.loading=false;
      const p=document.getElementById('D-edph');if(p)p.classList.remove('buffering');}})
    .catch(()=>{if(D.sa.key===key){D.sa.loading=false;
      const p=document.getElementById('D-edph');if(p)p.classList.remove('buffering');}});
}
// Load a fresh window only when the cursor nears/leaves the buffered one, so a
// slow drag reuses the buffer and only a big jump refetches.
function D_saEnsureAround(ms){
  const sa=D.sa;
  if(sa.buf&&ms>=sa.winStart+3000&&ms<=sa.winEnd-3000)return;
  D_saLoad(ms-SA_CAP/2,ms+SA_CAP/2);
}
// Stop the currently-sounding grain with a tiny fade (no click). Overlapping
// grains are what caused the echo, so a new grain always kills the old first —
// one voice follows the cursor.
function D_saKill(now){
  const sa=D.sa; if(!sa.node)return; const p=sa.node;
  try{p.g.gain.cancelScheduledValues(now);p.g.gain.setValueAtTime(Math.max(0.0001,p.g.gain.value),now);
      p.g.gain.linearRampToValueAtTime(0,now+0.02);p.src.stop(now+0.035);}catch(e){}
  sa.node=null;
}
function D_saStop(){const sa=D.sa;if(sa.ctx&&sa.node)D_saKill(sa.ctx.currentTime);}
function D_saGrain(ms){
  const sa=D.sa,ctx=sa.ctx;
  if(D.audioMode==='muted'||!ctx||!sa.buf)return;
  if(ms<sa.winStart||ms>sa.winEnd)return;
  const now=ctx.currentTime;
  if(now-sa.last<0.05)return;                        // ~20 grains/s
  sa.last=now;
  D_saKill(now);                                     // single voice → no overlap, no echo
  const off=Math.max(0,Math.min(sa.buf.duration-SA_GRAIN,(ms-sa.winStart)/1000));
  const src=ctx.createBufferSource(); src.buffer=sa.buf;
  const g=ctx.createGain(); src.connect(g); g.connect(ctx.destination);
  g.gain.setValueAtTime(0,now); g.gain.linearRampToValueAtTime(0.85,now+0.012);
  g.gain.setValueAtTime(0.85,now+SA_GRAIN-0.04); g.gain.linearRampToValueAtTime(0,now+SA_GRAIN);
  try{src.start(now,off,SA_GRAIN);}catch(e){}
  sa.node={src,g};
}
// One call per scrub move: keep a window buffered around the cursor and sound it.
function D_scrubAudio(ms){D_saEnsureAround(ms);D_saGrain(ms);}

// Phase 2 — whole-film continuous stream from the playhead. `/api/stream`
// transcodes the film [playMs, end] into a fragmented MP4 the <video> plays
// across scenes; Cleaned cuts every approved skip out and silences mutes live
// over the whole remaining film (time-compressed, mapped back through keeps).
function D_filmPlan(){
  const start=Math.max(0,Math.round(D.playMs));
  const base=`/api/stream?path=${encodeURIComponent(MEDIA)}&startMs=${start}`;
  if(D.audioMode==='cleaned'){
    const apply=SEGS.filter(x=>x.approved===true&&x.endMs>start);
    const span=x=>[Math.max(start,Math.round(x.startMs)),Math.round(x.endMs)];
    const cuts=apply.filter(x=>x.recommendedAction==='skip').map(span);
    // Live whole-film stream can't run Demucs per-frame, so voice-only mutes
    // fall back to a hard mute here (the word is gone). The scene preview shows
    // the true voice-only mute with music intact.
    const mutes=apply.filter(x=>x.recommendedAction==='mute'||x.recommendedAction==='voice').map(span);
    const blurs=apply.filter(x=>x.recommendedAction==='blur').map(span);
    const enc=sp=>sp.map(([a,b])=>a+'-'+b).join(',');
    return {src:base+`&cut=${enc(cuts)}&mute=${enc(mutes)}&blur=${enc(blurs)}`,
      keeps:D_buildKeeps(start,RUNTIME_MS,cuts), playWinStart:start, clipStartAbs:start};
  }
  return {src:base, keeps:null, playWinStart:0, clipStartAbs:start}; // t=0 == playMs
}
// Scene — the fast, cached per-window clip (Phase 1).
function D_scenePlan(s){
  if(D.audioMode==='cleaned'){
    const p=D_cleanedPlan(s);
    return {src:D_clipSrc(s), keeps:D_buildKeeps(p.winStart,p.winEnd,p.cuts),
      playWinStart:p.winStart, clipStartAbs:0};
  }
  return {src:D_clipSrc(s), keeps:null, playWinStart:0,
    clipStartAbs:Math.max(0,s.startMs-PLAY_PAD*1000)}; // clip t=0 in film ms
}
// Where the <video> currently sits, in film-time — used to tell a pause/resume
// (element still at the playhead) from a seek (playhead moved away → re-stream).
function D_elFilmMs(v){return D.keeps?D_clipToFilm(D.keeps,D.playWinStart,v.currentTime*1000):D.clipStartAbs+v.currentTime*1000;}
function D_play(){
  const v=document.getElementById('D-clip');
  if(D.loadingClip)return;                  // a build is already in flight — don't restart it
  if(D.playing){v.pause();return;}          // onpause flips state + repaints
  const film=D.range==='film';
  const s=D_get(D.sel);
  if(!film&&!s){D_ednote&&D_ednote('select a finding to play');return;}
  // Cheap resume: element already loaded at the playhead (paused, not seeked) —
  // just play, no rebuild/transcode. Scene keys on the src; film keys on whether
  // the element still sits where the playhead is (a click moves the playhead).
  if(v.readyState>=2&&v.paused){
    const resume=film?Math.abs(D_elFilmMs(v)-D.playMs)<500:(D.clipKey===D_scenePlan(s).src);
    if(resume){v.muted=D.audioMode==='muted';v.play().catch(()=>{});return;}
  }
  const plan=film?D_filmPlan():D_scenePlan(s);
  const src=plan.src;
  D.keeps=plan.keeps; D.playWinStart=plan.playWinStart; D.clipStartAbs=plan.clipStartAbs;
  D.clipKey=src; D.loadingClip=true;
  const cl=document.getElementById('D-cliploading');
  if(cl)cl.innerHTML='<span class="spin"></span> '+(film?'starting stream… (transcoding the film live)':'building clip… (transcoding this scene)');
  D_monitor();       // show the "building…" state
  v.src=src;
  v.muted=D.audioMode==='muted';
  v.onloadedmetadata=()=>{
    let t=0;
    if(!D.keeps&&!film){                                 // linear scene clip: resume from the playhead if in-window
      t=(D.playMs-D.clipStartAbs)/1000;
      if(!(t>0&&t<v.duration))t=Math.max(0,s.startMs/1000-D.clipStartAbs/1000-2);
    }
    try{v.currentTime=t;}catch(e){}                       // film stream begins at t=0 (== playMs)
    v.play().catch(()=>{});
  };
  v.oncanplay=()=>{D.loadingClip=false;D_monitor();};
  // Only follow while actually playing and not mid-seek: a trailing timeupdate
  // fired just after a pause/seek would otherwise yank the playhead back to the
  // clip position (why a timeline click "didn't take" until you paused).
  v.ontimeupdate=()=>{if(!D.playing||D.scrubbing)return;D_playFollow();};
  v.onplay=()=>{D.playing=true;D.loadingClip=false;D_monitor();};
  v.onpause=()=>{D.playing=false;D_monitor();};
  v.onended=()=>{D.playing=false;D_monitor();};
  v.onerror=()=>{D.playing=false;D.loadingClip=false;D.clipKey=null;D_ednote&&D_ednote('could not load clip');D_monitor();};
  v.load();   // kick the resource-selection algorithm so onloadedmetadata fires
}
function D_stop(){const v=document.getElementById('D-clip');if(v&&!v.paused)v.pause();D.playing=false;D_monitor();}
// Seek during (or into) playback; keeps the video in sync with the playhead.
function D_seek(d){
  D.playMs=Math.max(0,Math.min(RUNTIME_MS,D.playMs+d));
  const v=document.getElementById('D-clip');
  // While playing a *linear* clip we can seek the element to stay in sync; a
  // cleaned clip's time is compressed (skips removed), so a linear seek would
  // land wrong — just stop and move the playhead there instead.
  if(D.playing){
    if(!D.keeps&&v&&isFinite(v.duration)){const t=(D.playMs-D.clipStartAbs)/1000;
      if(t>=0&&t<=v.duration){v.currentTime=t;D_monitor();D_filmtl();D_editor();return;}}
    D_stop();
  }
  D_monitor();D_filmtl();D_editor();
}

// ---------- keyboard ----------
// ---------- the clock: type an exact time to jump there ----------
// Dragging gets you near a moment; a word mute needs the moment itself. Typing
// "1:37.950" is the only way to land on it exactly.
// Land the playhead on an absolute time, the way a click on the full-film bar
// does: pan the editor if the moment is off-screen, and select the finding it
// falls nearest, so the editor is showing the place you asked for.
function D_jumpTo(ms){
  if(D.playing)D_stop();
  D.playMs=Math.max(0,Math.min(RUNTIME_MS,ms));
  if(D.playMs<D.viewStart||D.playMs>D.viewEnd){
    const span=D.viewEnd-D.viewStart;
    D.viewStart=D.playMs-span/2; D.viewEnd=D.viewStart+span; D_clampView();
  }
  const n=D_nearest(D.playMs); if(n)D.sel=n.id;
  D_render();
}

function D_clockEdit(){
  const tt=document.getElementById('D-tt');
  if(tt.querySelector('input'))return;
  const inp=document.createElement('input');
  inp.value=fmtHMS(D.playMs); inp.spellcheck=false;
  inp.title='H:MM:SS.mmm, MM:SS.mmm or seconds';
  tt.innerHTML=''; tt.appendChild(inp);
  tt.append(` / ${fmtShort(RUNTIME_MS)}`);
  inp.focus(); inp.select();
  let done=false;
  const finish=commit=>{
    if(done)return; done=true;                 // blur fires again after Enter
    const ms=commit?parseTime(inp.value):null;
    if(ms!=null)D_jumpTo(ms); else D_monitor(); // D_jumpTo redraws; else restore
  };
  inp.onkeydown=e=>{
    if(e.key==='Enter'){e.preventDefault();finish(true);}
    else if(e.key==='Escape'){e.preventDefault();finish(false);}
    e.stopPropagation();                        // j/k/space are page shortcuts
  };
  inp.onblur=()=>finish(true);
}

// ---------- render a clean copy, without leaving this page ----------
// The findings you just approved are the whole input to a render, so the button
// belongs beside them. An existing clean copy is a file someone may be part-way
// through watching, so the choice to overwrite it is made here, in the open.
function D_renderClick(){
  const btn=document.getElementById('D-render');
  btn.disabled=true;
  fetch(`/api/render/plan?path=${encodeURIComponent(MEDIA)}`)
    .then(r=>r.ok?r.json():Promise.reject(new Error('could not read the render plan')))
    .then(plan=>{btn.disabled=false;D_renderDialog(plan);})
    .catch(e=>{btn.disabled=false;alert(e.message||'could not reach the worker');});
}

function D_renderDialog(plan){
  const approved=SEGS.filter(s=>s.approved===true).length;
  const ov=document.createElement('div');
  ov.className='rov';
  const opts=plan.replaceExists
    ? `<button class="ropt on" data-mode="replace"><b>Overwrite the ${esc(plan.replaceLabel)} copy</b>
         <span>${esc(plan.replacePath)}</span></button>
       <button class="ropt" data-mode="new"><b>Keep it, add a ${esc(plan.newLabel)} copy</b>
         <span>${esc(plan.newPath)}</span></button>`
    : `<button class="ropt on" data-mode="replace"><b>Write the ${esc(plan.newLabel)} copy</b>
         <span>${esc(plan.replacePath)}</span></button>`;
  ov.innerHTML=`<div class="rdlg">
    <h2>Render a clean copy</h2>
    <div class="rsub">${approved} approved finding${approved===1?'':'s'} will be applied${
      plan.sourceIsCleanCopy?`, read from ${esc(plan.sourceName)}`:''}.</div>
    ${opts}
    <div class="rmsg" id="D-rmsg"></div>
    <div class="rfoot"><button class="x">Cancel</button><button class="go">Start render</button></div>
  </div>`;
  document.body.appendChild(ov);
  let mode=plan.replaceExists?'replace':'replace';
  const close=()=>{if(ov.parentNode)ov.parentNode.removeChild(ov);document.removeEventListener('keydown',onKey);};
  const onKey=e=>{if(e.key==='Escape'){e.preventDefault();close();}e.stopPropagation();};
  document.addEventListener('keydown',onKey);
  ov.onclick=e=>{if(e.target===ov)close();};
  ov.querySelector('.x').onclick=close;
  ov.querySelectorAll('.ropt').forEach(b=>b.onclick=()=>{
    mode=b.dataset.mode;
    ov.querySelectorAll('.ropt').forEach(x=>x.classList.toggle('on',x===b));
  });
  const go=ov.querySelector('.go');
  go.onclick=()=>{
    go.disabled=true; go.textContent='Starting…';
    fetch(`/api/render?path=${encodeURIComponent(MEDIA)}`,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})})
      .then(r=>r.json().then(body=>r.ok?body:Promise.reject(new Error(body.detail||'the worker refused the render'))))
      .then(job=>{close();D_renderWatch(job.id);})
      .catch(e=>{
        go.disabled=false; go.textContent='Start render';
        ov.querySelector('#D-rmsg').textContent=e.message||'could not start the render';
      });
  };
}

// A render runs for a long time, so the button becomes the progress readout —
// nothing to keep open, and it survives a reload by re-finding the job.
function D_renderWatch(jobId){
  const btn=document.getElementById('D-render');
  btn.classList.add('busy'); btn.disabled=true;
  const tick=()=>{
    fetch(`/api/jobs/${encodeURIComponent(jobId)}`).then(r=>r.json()).then(job=>{
      if(job.status==='rendered'){
        btn.classList.remove('busy'); btn.disabled=false;
        btn.textContent='✓ Clean copy written';
        setTimeout(()=>{btn.textContent='🎬 Render clean copy';},6000);
        return;
      }
      if(job.status==='failed'||job.status==='cancelled'){
        btn.classList.remove('busy'); btn.disabled=false;
        btn.textContent='🎬 Render clean copy';
        alert(`Render ${job.status}: ${job.error||'no reason given'}`);
        return;
      }
      const pct=Math.round((job.progress||0)*100);
      btn.textContent=`⏳ Rendering ${pct}%`;
      btn.title=job.stage||'';
      setTimeout(tick,2000);
    }).catch(()=>setTimeout(tick,5000));   // a blip must not abandon the render
  };
  tick();
}

// ---------- library switcher (top-left combobox) ----------
let SW={open:false,q:'',items:[],active:-1,timer:null,total:0};
function D_swOpen(){SW.open=true;document.getElementById('D-swpanel').classList.remove('hidden');
  const inp=document.getElementById('D-swinput');inp.value=SW.q;inp.focus();inp.select();D_swLoad(SW.q);}
function D_swClose(){SW.open=false;document.getElementById('D-swpanel').classList.add('hidden');}
function D_swToggle(){SW.open?D_swClose():D_swOpen();}
function D_swLoad(q){
  const tok=(SW.q=q);
  const head=document.getElementById('D-swhead'),list=document.getElementById('D-swlist');
  if(head)head.textContent='';
  if(list)list.innerHTML='<div class="swempty"><span class="spin"></span>Loading…</div>';
  fetch(`/api/library?q=${encodeURIComponent(q)}&limit=60`).then(r=>r.json()).then(d=>{
    if(SW.q!==tok)return;                 // a newer keystroke already fired
    SW.items=d.items||[];SW.total=d.total||0;SW.active=SW.items.length?0:-1;D_swRender();
  }).catch(()=>{if(SW.q!==tok)return;SW.items=[];SW.total=0;D_swRender();});
}
function D_swBadge(it){
  const lbl={ready:it.undecidedCount+' to review',in_progress:it.undecidedCount+' left',
    reviewed:'reviewed ✓',unanalyzed:'not analyzed',corrupt:'⚠ unreadable'}[it.status]||it.status;
  return `<span class="swbadge ${it.status}">${lbl}</span>`;
}
function D_swRender(){
  const head=document.getElementById('D-swhead'),list=document.getElementById('D-swlist');
  if(!SW.q){const rev=SW.items.filter(x=>x.status==='reviewed').length;
    head.textContent=`${SW.items.length-rev} to review · ${rev} reviewed`;}
  else head.textContent=`${SW.total} match${SW.total===1?'':'es'}`;
  if(!SW.items.length){
    list.innerHTML=`<div class="swempty">${SW.q?'No video matches “'+esc(SW.q)+'”.'
      :'Nothing analyzed yet — search a title to open it for manual review.'}</div>`;return;}
  list.innerHTML=SW.items.map((it,i)=>{
    const cur=it.path===MEDIA;
    const analyze=it.status==='unanalyzed'?`<span class="swanalyze" data-act="analyze">Analyze</span>`:'';
    return `<div class="swrow ${i===SW.active?'active':''}" data-i="${i}">
      <div class="swmeta"><div class="swn">${cur?'<span class="swcur-dot">● </span>':''}${esc(it.name)}</div>
      <div class="swc">${esc(it.collection)}</div></div>${D_swBadge(it)}${analyze}</div>`;
  }).join('');
  list.querySelectorAll('.swrow').forEach(row=>row.onclick=e=>{
    const i=+row.dataset.i;
    const an=e.target.closest('.swanalyze');
    if(an){e.stopPropagation();D_swAnalyze(an,SW.items[i]);return;}
    D_swPick(SW.items[i]);
  });
}
// Open ANY video for review — even unanalyzed (empty Studio, review by hand).
function D_swPick(it){location.href='/api/review?path='+encodeURIComponent(it.path);}
function D_swAnalyze(el,it){
  if(!confirm('Queue analysis (profanity + visual) for “'+it.name+'”?\nThe visual pass is GPU-heavy and can take hours.'))return;
  el.textContent='Queuing…';el.classList.add('busy');
  Promise.all(['subtitles','vlm'].map(engine=>fetch('/api/jobs',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({mediaPath:it.path,engine})})))
    .then(()=>D_swLoad(SW.q)).catch(()=>{el.textContent='Analyze';el.classList.remove('busy');});
}
function D_swScroll(){const el=document.querySelector('#D-swlist .swrow.active');if(el)el.scrollIntoView({block:'nearest'});}
function D_swKey(e){
  if(e.key==='ArrowDown'){e.preventDefault();SW.active=Math.min(SW.items.length-1,SW.active+1);D_swRender();D_swScroll();}
  else if(e.key==='ArrowUp'){e.preventDefault();SW.active=Math.max(0,SW.active-1);D_swRender();D_swScroll();}
  else if(e.key==='Enter'){e.preventDefault();if(SW.active>=0)D_swPick(SW.items[SW.active]);}
  else if(e.key==='Escape'){e.preventDefault();D_swClose();document.getElementById('D-swtrigger').focus();}
}

function D_key(e){const k=e.key.toLowerCase();const s=D_get(D.sel);
  if(e.key==='/'){e.preventDefault();D_swOpen();return;}
  if(k===' '){e.preventDefault();D_play();return;}
  if(k==='j'||k==='k'){if(!SEGS.length)return;const i=SEGS.findIndex(x=>x.id===D.sel);
    const n=SEGS[(i+(k==='j'?1:-1)+SEGS.length)%SEGS.length];D_select(n.id);return;}
  if(k==='a'){e.preventDefault();D_add();return;}
  if(k==='s'){e.preventDefault();D_split();return;}
  if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();D_delRegion();return;}
  if(k==='c'){if(s)D_decide(s,true);return;}
  if(k==='l'){if(s)D_decide(s,false);return;}
}
document.addEventListener('keydown',e=>{
  const tag=(document.activeElement?.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea'||tag==='select')return;
  D_key(e);
});

// ---------- init ----------
function D_init(){
  // library switcher (top-left combobox)
  document.getElementById('D-swtrigger').onclick=D_swToggle;
  const swin=document.getElementById('D-swinput');
  swin.oninput=()=>{clearTimeout(SW.timer);const q=swin.value;SW.timer=setTimeout(()=>D_swLoad(q),180);};
  swin.onkeydown=D_swKey;
  document.addEventListener('pointerdown',e=>{if(SW.open&&!e.target.closest('#D-switcher'))D_swClose();});
  document.getElementById('D-discreet').onclick=()=>{D.discreet=!D.discreet;
    document.getElementById('D-discreet').classList.toggle('on',D.discreet);D_monitor();};
  const rev=document.getElementById('D-reveal');
  rev.onpointerdown=()=>{D.held=true;D_monitor();};
  ['pointerup','pointerleave','pointercancel'].forEach(ev=>rev.addEventListener(ev,()=>{D.held=false;D_monitor();}));
  document.getElementById('D-pp').onclick=D_play;
  document.getElementById('D-back1').onclick=()=>D_seek(-1000);
  document.getElementById('D-fwd1').onclick=()=>D_seek(1000);
  // mobile tab switch: Player <-> Findings (no-op above the breakpoint, see CSS)
  document.getElementById('D-mtab-stage').onclick=()=>D_mtabSet('stage');
  document.getElementById('D-mtab-findings').onclick=()=>D_mtabSet('findings');
  // picture mode (Visual ↔ Video) and audio mode (Normal / Cleaned / Muted)
  document.querySelectorAll('#D-picmode button').forEach(b=>b.onclick=()=>{
    D.picMode=b.dataset.pic;
    document.querySelectorAll('#D-picmode button').forEach(x=>x.classList.toggle('on',x===b));
    D_monitor();});
  document.querySelectorAll('#D-audmode button').forEach(b=>b.onclick=()=>{
    D.audioMode=b.dataset.aud;
    document.querySelectorAll('#D-audmode button').forEach(x=>x.classList.toggle('on',x===b));
    const v=document.getElementById('D-clip');
    v.muted=D.audioMode==='muted';
    if(D.playing)D_stop();   // the clip's flags change with the mode; press play to hear it anew
  });
  // range (Scene ↔ Film): Scene plays the selected finding's window (fast,
  // cached); Film streams the whole film continuously from the playhead.
  document.querySelectorAll('#D-range button').forEach(b=>b.onclick=()=>{
    D.range=b.dataset.range;
    document.querySelectorAll('#D-range button').forEach(x=>x.classList.toggle('on',x===b));
    D.clipKey=null;          // a new range means a different source — don't reuse the old clip
    if(D.playing)D_stop();
  });
  document.getElementById('D-bulkcut').onclick=()=>D_bulk(true);
  document.getElementById('D-bulkleave').onclick=()=>D_bulk(false);
  // minimap background = seek; recenter the viewport if you land outside it.
  document.getElementById('D-ftrack').addEventListener('pointerdown',e=>{
    if(e.target.id==='D-fbox')return;
    if(D.playing)D_stop();   // a click on the full-film bar seeks — it must win over playback
    const track=e.currentTarget, at=cx=>{const r=track.getBoundingClientRect();return Math.max(0,Math.min(RUNTIME_MS,(cx-r.left)/r.width*RUNTIME_MS));};
    D.scrubbing=true; D_saCtx();   // prime the audio context on this gesture
    D.playMs=at(e.clientX);D_scrubAudio(D.playMs);D_monitor();D_filmtl();D_highlightNearest();
    const move=ev=>{D.playMs=at(ev.clientX);D_scrubAudio(D.playMs);D_monitor();D_filmtl();D_highlightNearest();};
    const up=()=>{D.scrubbing=false;D_saStop();document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);
      if(D.playMs<D.viewStart||D.playMs>D.viewEnd){const span=D.viewEnd-D.viewStart;D.viewStart=D.playMs-span/2;D.viewEnd=D.viewStart+span;D_clampView();}
      const n=D_nearest(D.playMs);if(n)D.sel=n.id;D_render();};
    document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);});
  // drag the viewport box to pan the editor below
  document.getElementById('D-fbox').addEventListener('pointerdown',e=>{e.stopPropagation();e.preventDefault();
    const track=document.getElementById('D-ftrack'),rect=track.getBoundingClientRect();
    const span=D.viewEnd-D.viewStart, sx=e.clientX, vs0=D.viewStart;
    const move=ev=>{D.viewStart=vs0+(ev.clientX-sx)/rect.width*RUNTIME_MS;D.viewEnd=D.viewStart+span;D_clampView();D_filmtl();D_editor();};
    const up=()=>{document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);D_render();};
    document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);});
  document.getElementById('D-render').onclick=D_renderClick;
  document.getElementById('D-tt').onclick=e=>{if(e.target.classList.contains('clock'))D_clockEdit();};
  document.getElementById('D-mergemode').onclick=()=>{D.merge=!D.merge;if(!D.merge)D.picks.clear();D_render();};
  document.getElementById('D-mergego').onclick=D_doMerge;
  document.getElementById('D-mergeclear').onclick=()=>{D.picks.clear();D_render();};
  D_mtabSet('stage');   // initial mobile pane
}

D_init();
D_render();
</script>
"""


def render_page(media: Path, timeline: Timeline) -> str:
    return (
        PAGE.replace("__TITLE__", _html_escape(media.stem))
        .replace("__PATH_DISPLAY__", _html_escape(str(media)))
        .replace("__MEDIA_JSON__", json.dumps(str(media)))
        .replace("__PAD__", f"{CLIP_PAD_S:.0f}")
        .replace("__RUNTIME_MS__", str(media_runtime_ms(media, timeline)))
        .replace(
            "__SEGS_JSON__",
            json.dumps([s.model_dump() for s in timeline.segments]),
        )
    )


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# A path-less landing: browse/search the whole library and open any film in the
# Studio. Served by GET /api/review with no ?path= — the entry point the plugin
# links to so a reviewer can start from any video, analyzed or not.
LANDING = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clean Media — review</title>
<style>
:root{--bg:#0d1013;--panel:#16191d;--panel2:#1d2126;--line:#2a2f36;--ink:#e6edf3;--dim:#9aa5b1;--dim2:#6e7681;--pick:#3b82f6;font-family:system-ui,-apple-system,Segoe UI,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:8vh 16px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--dim2);font-size:13px;margin-bottom:20px}
.wrap{width:min(620px,94vw)}
input{width:100%;box-sizing:border-box;background:#0d1117;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:13px 15px;font:inherit;font-size:15px;outline:none}
input:focus{border-color:var(--pick)}
.head{padding:9px 4px;font-size:11.5px;color:var(--dim2)}
.list{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;max-height:64vh;overflow-y:auto}
.row{display:flex;align-items:center;gap:10px;padding:11px 14px;cursor:pointer;border-bottom:1px solid #ffffff08}
.row:hover,.row.active{background:var(--panel2)}
.meta{flex:1;min-width:0}.n{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.c{font-size:11.5px;color:var(--dim2);margin-top:1px}
.badge{flex:0 0 auto;font-size:10.5px;font-weight:700;padding:3px 9px;border-radius:99px}
.badge.ready{background:#3a2c12;color:#f0c05a}.badge.in_progress{background:#152a3d;color:#7cc0ff}
.badge.reviewed{background:#12331f;color:#5ee27f}.badge.unanalyzed{background:#22262c;color:var(--dim)}
.badge.corrupt{background:#3a1414;color:#ff6e6e}
.empty{padding:18px 14px;color:var(--dim2);font-size:13px;line-height:1.5}
.spin{display:inline-block;width:12px;height:12px;border-radius:99px;border:2px solid currentColor;
  border-top-color:transparent;opacity:.7;animation:review-spin .7s linear infinite;vertical-align:-2px;margin-right:7px}
@keyframes review-spin{to{transform:rotate(360deg)}}
::-webkit-scrollbar{width:12px}::-webkit-scrollbar-thumb{background:#39424e;border:3px solid transparent;background-clip:padding-box;border-radius:99px}
</style>
<div class="wrap">
  <h1>Clean Media — review</h1>
  <div class="sub">Pick a video to review. Search finds any film in any collection; unanalyzed ones open for manual review.</div>
  <input id="q" placeholder="Search any video in any collection…" autocomplete="off" spellcheck="false" autofocus>
  <div class="head" id="head"></div>
  <div class="list" id="list"></div>
</div>
<script>
let items=[],active=-1,q='',timer=null,total=0;
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function badge(it){const l={ready:it.undecidedCount+' to review',in_progress:it.undecidedCount+' left',reviewed:'reviewed ✓',unanalyzed:'not analyzed',corrupt:'⚠ unreadable'}[it.status]||it.status;return `<span class="badge ${it.status}">${l}</span>`;}
function load(query){const tok=(q=query);
  document.getElementById('list').innerHTML='<div class="empty"><span class="spin"></span>Loading…</div>';
  document.getElementById('head').textContent='';
  fetch(`/api/library?q=${encodeURIComponent(query)}&limit=80`).then(r=>r.json()).then(d=>{if(q!==tok)return;items=d.items||[];total=d.total||0;active=items.length?0:-1;render();}).catch(()=>{if(q!==tok)return;items=[];render();});}
function render(){const head=document.getElementById('head'),list=document.getElementById('list');
  head.textContent=q?`${total} match${total===1?'':'es'}`:`${items.filter(x=>x.status!=='reviewed').length} to review · ${items.filter(x=>x.status==='reviewed').length} reviewed`;
  if(!items.length){list.innerHTML=`<div class="empty">${q?'No video matches “'+esc(q)+'”.':'Nothing analyzed yet — search a title to open it for manual review.'}</div>`;return;}
  list.innerHTML=items.map((it,i)=>`<div class="row ${i===active?'active':''}" data-i="${i}"><div class="meta"><div class="n">${esc(it.name)}</div><div class="c">${esc(it.collection)}</div></div>${badge(it)}</div>`).join('');
  list.querySelectorAll('.row').forEach(r=>r.onclick=()=>open(items[+r.dataset.i]));}
function open(it){location.href='/api/review?path='+encodeURIComponent(it.path);}
const inp=document.getElementById('q');
inp.oninput=()=>{clearTimeout(timer);const v=inp.value;timer=setTimeout(()=>load(v),180);};
inp.onkeydown=e=>{if(e.key==='ArrowDown'){e.preventDefault();active=Math.min(items.length-1,active+1);render();}
  else if(e.key==='ArrowUp'){e.preventDefault();active=Math.max(0,active-1);render();}
  else if(e.key==='Enter'){e.preventDefault();if(active>=0)open(items[active]);}};
load('');
</script>
"""


def render_landing() -> str:
    return LANDING
