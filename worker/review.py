"""Administrator review UI.

Serves a page of every finding for a film with a thumbnail, and writes
approve/reject decisions straight back to the `.cleanmedia.json` sidecar.

That sidecar is what `GET /api/segments` reads, and the Jellyfin plugin
requests `approvedOnly=true` — so approving a finding here is what makes
Jellyfin skip it. Rejecting it makes it disappear from playback and from
any future render. Nothing acts on a finding until it is approved.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from .models import Segment, Timeline
from .store import media_fingerprint

THUMB_WIDTH = 480
CLIP_PAD_S = 15.0

#: Engine identity for findings an administrator added by hand. Never runs,
#: so a merge always keeps its segments.
MANUAL_ENGINE = "manual"


def sidecar_for(media: Path) -> Path:
    return media.with_name(media.stem + ".cleanmedia.json")


def media_roots() -> list[Path]:
    """Directories to search when a caller's path does not exist locally."""
    configured = os.environ.get("CLEANMEDIA_MEDIA_ROOTS", "")
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
#: The CLEANMEDIA_MEDIA_ROOTS the cache was built for. Comparing this (a cheap
#: string, no I/O) means a roots change — a reconfig, or a different root per
#: test — rebuilds the index instead of serving a stale one.
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
        sig = os.environ.get("CLEANMEDIA_MEDIA_ROOTS", "")
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


def load_timeline(media: Path) -> Optional[Timeline]:
    path = sidecar_for(media)
    if not path.is_file():
        return None
    return Timeline.model_validate_json(path.read_text(encoding="utf-8"))


def save_timeline(media: Path, timeline: Timeline) -> None:
    """Write the sidecar. This is the record of every review decision."""
    sidecar_for(media).write_text(
        json.dumps(timeline.model_dump(), indent=2), encoding="utf-8"
    )


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
    """
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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
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
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
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


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Review — {title}</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:24px}}
 h1{{font-weight:600;margin:0 0 4px}}
 .sub{{color:#9aa;margin-bottom:12px}}
 /* flex-start, or a tall card stretches every neighbour in its row */
 .grid{{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}}
 .cell{{width:440px;background:#1d1d1f;border-radius:10px;overflow:hidden;
        border:2px solid transparent}}
 .cell.approved{{border-color:#3fb950}} .cell.rejected{{border-color:#f85149;opacity:.55}}
 .cell.tentative{{background:#20201a}}
 .cell.hidden{{display:none}}
 .tag{{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
       font-weight:700;background:#7a5c00;color:#ffd479;margin-left:6px}}
 .tag.est{{background:#5a2d00;color:#ffb877}} .tag.exact{{background:#12361f;color:#7ee787}}
 /* the box keeps its size whatever is inside, so swapping the thumbnail
    for a loading state or a video never makes the page jump */
 .shot{{position:relative;cursor:pointer;aspect-ratio:16/9;background:#000}}
 .cell img,.cell video{{width:100%;height:100%;display:block;
                        object-fit:contain;background:#000}}
 .play{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
        background:rgba(0,0,0,.35);color:#fff;font-size:15px;font-weight:600}}
 .play span{{background:rgba(0,0,0,.72);padding:9px 16px;border-radius:99px}}
 /* preview cut-marker: a strip over the top of the video showing the flagged
    span (the part acted on) and a playhead, so it is clear what is cut. */
 .cliptl{{position:absolute;left:0;right:0;top:0;height:18px;z-index:2;
          background:rgba(0,0,0,.5);pointer-events:none;font-size:11px}}
 .cliptl .cutband{{position:absolute;top:0;bottom:0;background:rgba(248,81,73,.4);
                   border-left:2px solid #f85149;border-right:2px solid #f85149}}
 .cliptl.skip .cutband{{border-color:#ffb877;background:repeating-linear-gradient(
     45deg,rgba(255,184,119,.5) 0 6px,rgba(255,184,119,.15) 6px 12px)}}
 .cliptl.flash .cutband{{background:rgba(255,255,255,.75);transition:background .1s}}
 .cliptl .ph{{position:absolute;top:0;bottom:0;width:2px;background:#fff;
              box-shadow:0 0 3px #000}}
 .cliptl .cutlbl{{position:absolute;left:6px;top:2px;color:#ffd0cb;font-weight:600;
                  text-shadow:0 1px 2px #000;white-space:nowrap}}
 .hint{{color:#8b949e;font-size:11px;padding:0 12px 8px}}
 .meta{{padding:10px 12px;font-size:13px;line-height:1.55}}
 .cat{{color:#ff8f6b;font-weight:600}} .act{{color:#7ee787}}
 .word{{color:#ffd479;font-weight:600}}
 .why{{color:#9aa;font-style:italic;cursor:text;border-radius:4px;
       padding:1px 3px;margin:0 -3px}}
 .why:hover{{background:#1b1f24}}
 .why:focus{{outline:none;font-style:normal;color:#e6edf3;background:#0d1117;
             box-shadow:0 0 0 1px #30588c}}
 .clipbtns{{display:flex;gap:8px;padding:0 12px 8px}}
 .clipbtns button{{background:#22262c;font-size:12px;padding:7px}}
 .btns{{display:flex;gap:8px;padding:0 12px 12px}}
 button{{flex:1;padding:9px;border:0;border-radius:6px;font-size:13px;
         font-weight:600;cursor:pointer;background:#30363d;color:#eee}}
 button.yes.on{{background:#238636}} button.no.on{{background:#da3633}}
 .btns select{{flex:0 0 auto;padding:9px;border:0;border-radius:6px;background:#30363d;
               color:#eee;font-size:13px;font-weight:600;cursor:pointer}}
 .editor{{padding:0 12px 12px;display:none}} .editor.open{{display:block}}
 .wavewrap{{overflow-x:auto;background:#0c0c0d;border:1px solid #2a2f36;border-radius:6px}}
 .wavebox{{position:relative}} .wavebox canvas,.wavebox .strip{{display:block}}
 .strip{{object-fit:fill;image-rendering:auto}}
 .wavebox{{cursor:crosshair}}
 .region{{position:absolute;top:0;bottom:0;background:rgba(255,212,121,.15);pointer-events:none}}
 /* a scratch selection to audition a spot, separate from the segment bounds */
 .selregion{{position:absolute;top:0;bottom:0;background:rgba(88,166,255,.22);
             border-left:2px solid #58a6ff;border-right:2px solid #58a6ff;
             pointer-events:none;display:none}}
 .handle{{position:absolute;top:0;bottom:0;width:2px;background:#ffd479;pointer-events:none}}
 .handle.end{{background:#7ee787}}
 .handle .grip{{position:absolute;top:0;left:-6px;width:14px;height:18px;border-radius:3px;
                background:inherit;pointer-events:auto;cursor:ew-resize}}
 .tctrls{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px;
          font-size:12px;color:#9aa}}
 .tctrls button{{flex:0 0 auto;padding:6px 10px;background:#22262c;font-size:12px}}
 .tctrls button.tsave{{background:#238636;color:#fff}}
 .selread{{font-size:12px;color:#9aa;margin-left:8px;font-variant-numeric:tabular-nums}}
 .selread b{{color:#e6edf3}}
 .seluse{{flex:0 0 auto;padding:4px 9px;background:#30588c;color:#fff;font-size:11px;margin-left:8px}}
 .tlabel{{display:inline-block;min-width:34px}}
 .tread{{font-variant-numeric:tabular-nums;color:#eee;font-weight:600}}
 .thint{{opacity:.6}} .tload{{padding:12px;color:#9aa;font-size:12px}}
 /* editable time fields (timing editor + add-segment form) */
 .tinput{{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;
          padding:3px 6px;font:inherit;font-size:12px;font-variant-numeric:tabular-nums}}
 .tinput:focus{{outline:none;border-color:#30588c}}
 #addbar button{{background:#30588c;color:#fff}}
 #addbar .tosep{{margin:0 4px;color:#8b949e}}
 #addbar .mhint{{font-size:12px;color:#8b949e;margin-left:8px}}
 .clipbtns button.dup{{margin-left:auto}}
 .clipbtns button.del{{color:#ff7b72}}
 .bar{{position:sticky;top:0;background:#111;padding:10px 0;margin-bottom:10px;z-index:9}}
 .filters{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}}
 .filters button{{flex:0 0 auto;padding:6px 12px;background:#22262c;color:#9aa;font-size:12px}}
 .filters button.on{{background:#30588c;color:#fff}}
 .filters label{{font-size:12px;color:#9aa;margin-left:8px;cursor:pointer;user-select:none}}
 .flabel{{font-size:12px;color:#8b949e;font-weight:600;margin-right:2px}}
 .filters .count{{opacity:.65;font-weight:400}}
 /* the bulk row acts on whatever is shown, so keep it visually apart */
 #bulk{{border-top:1px solid #2a2f36;padding-top:8px}}
 #bulk button.set-yes{{background:#238636;color:#fff}}
 #bulk button.set-no{{background:#da3633;color:#fff}}
 #bulk button.set-clear{{background:#3a3f47;color:#eee}}
 #bulk button:disabled{{opacity:.4;cursor:default}}
 /* merge: pick 2+ findings and collapse them into one segment */
 #mergebar button{{background:#30588c;color:#fff}}
 #mergebar button:disabled{{opacity:.4;cursor:default}}
 #mergebar .mhint{{font-size:12px;color:#8b949e;margin-left:8px}}
 .mergepick{{font-size:11px;color:#8b949e;cursor:pointer;user-select:none;float:right}}
 .mergepick input{{vertical-align:middle;margin:0 3px 0 0}}
 .cell.picked{{outline:2px solid #30588c}}
</style>
<div class=bar>
  <h1>{title}</h1>
  <div class=sub id=summary>{count} finding(s) — approve what should be acted on</div>
  <div class=filters>
    <span class=flabel>Decision:</span>
    <button data-f=all class=on>All</button>
    <button data-f=undecided>Undecided</button>
    <button data-f=approved>Approved</button>
    <button data-f=rejected>Rejected</button>
    <label><input type=checkbox id=tight> Play flagged part only</label>
  </div>
  <div class=filters id=typeFilters>
    <span class=flabel>Type:</span>
  </div>
  <div class=filters id=bulk>
    <span class=flabel>Apply to all <b id=shownCount>0</b> shown:</span>
    <button class=set-yes>Bad — act on all</button>
    <button class=set-no>Fine — ignore all</button>
    <button class=set-clear>Reset all</button>
  </div>
  <div class=filters id=mergebar>
    <span class=flabel>Merge into one:</span>
    <select id=mergeAction>
      <option value=skip selected>skip</option>
      <option value=mute>mute</option>
      <option value=voice>voice-only mute</option>
      <option value=blur>blur</option>
    </select>
    <button id=mergeBtn disabled>Merge selected (0)</button>
    <span class=mhint>tick “merge” on 2+ findings to combine them into one segment spanning them all</span>
  </div>
  <div class=filters id=addbar>
    <span class=flabel>Add:</span>
    <button id=addToggle>&#43; Add segment</button>
    <span id=addform style="display:none">
      <input id=addStart class=tinput size=11 placeholder="0:00:00.000">
      <span class=tosep>to</span>
      <input id=addEnd class=tinput size=11 placeholder="0:00:00.000">
      <select id=addAction>
        <option value=skip selected>skip</option>
        <option value=mute>mute</option>
        <option value=voice>voice-only mute</option>
        <option value=blur>blur</option>
      </select>
      <input id=addNote class=tinput size=22 placeholder="description (optional)">
      <button id=addSave>Add</button>
      <button id=addCancel>Cancel</button>
      <span class=mhint>times as H:MM:SS.mmm</span>
    </span>
  </div>
</div>
<div class=grid id=grid></div>
<script>
const MEDIA = {media!r};
const segs = {segments};
const PAD = {pad:.0f};

// A weaker signal the model was told to raise rather than force a guess on.
const TENTATIVE = ['suggestive'];
let filter = 'all';
let typeFilter = 'all';
// Ids ticked for merging. Merge collapses them into one segment spanning all.
const selected = new Set();

// The reviewer-facing "type" of a finding: for profanity that is the muted
// word — each word its own group — and otherwise the category. This is what
// the Type filter row groups and counts by.
function typeOf(s) {{ return word(s) || s.category; }}

const fmt = ms => {{ const x = ms/1000;
  return `${{Math.floor(x/3600)}}:${{String(Math.floor(x%3600/60)).padStart(2,'0')}}:${{(x%60).toFixed(1).padStart(4,'0')}}`; }};

// The timing tier the subtitle engine recorded, in parentheses in the
// reasoning. Everything but 'estimated' is a real, to-the-word timing.
function timing(s) {{
  const m = /\\((single-word-cue|cached-asr|retranscribed|estimated|whole-cue)\\)/.exec(s.reasoning||'');
  if (!m) return null;
  const exact = m[1] !== 'estimated';
  return {{exact, label: exact ? 'exact timing' : 'approx — ASR could not place the word'}};
}}
// The muted word, from the [word] the subtitle engine prefixes.
function word(s) {{ const m = /^\\[([^\\]]+)\\]/.exec(s.reasoning||''); return m ? m[1] : null; }}

function playClip(box, s, mute, voice, padOverride, skip) {{
  box.innerHTML = '<div class=play><span>'
    + (voice ? 'separating voice&hellip; (a few seconds)' : 'loading clip&hellip;')
    + '</span></div>';
  const tight = document.getElementById('tight').checked;
  // padOverride lets an audition play just the selected span, not padded ±PAD.
  const pad = padOverride != null ? padOverride : (tight ? 2 : PAD);
  // Where the flagged span sits inside the padded clip (seconds from clip start).
  const clipStart = Math.max(0, s.startMs / 1000 - pad);
  const cutStart = s.startMs / 1000 - clipStart;
  const cutEnd = s.endMs / 1000 - clipStart;
  // Mark the cut only on full previews, not the tiny waveform auditions.
  const showBar = padOverride == null;

  const v = document.createElement('video');
  v.controls = true; v.autoplay = true; v.preload = 'auto';
  v.src = `/api/clip?path=${{encodeURIComponent(MEDIA)}}`
        + `&startMs=${{s.startMs}}&endMs=${{s.endMs}}&pad=${{pad}}`
        + (mute ? '&mute=true' : '') + (voice ? '&voice=true' : '');

  // A strip over the top of the video marks the flagged span — the part being
  // cut, muted or blurred — with a playhead, so it is clear exactly what the
  // finding covers. In skip mode the strip is hatched and playback jumps the
  // span, the way Jellyfin skips the scene during playback.
  let tl, band, ph;
  if (showBar) {{
    tl = document.createElement('div');
    tl.className = 'cliptl' + (skip ? ' skip' : '');
    tl.innerHTML = '<div class=cutband></div><div class=ph></div><div class=cutlbl></div>';
    band = tl.querySelector('.cutband'); ph = tl.querySelector('.ph');
    tl.querySelector('.cutlbl').textContent =
      (skip ? 'skips ' : 'flagged ') + fmt(s.startMs) + ' - ' + fmt(s.endMs);
  }}

  v.onloadedmetadata = () => {{
    box.innerHTML = ''; box.appendChild(v);
    const dur = v.duration || (cutEnd - cutStart + 2 * pad);
    if (showBar) {{
      box.appendChild(tl);
      band.style.left = (100 * cutStart / dur) + '%';
      band.style.width = (100 * Math.max(0.3, cutEnd - cutStart) / dur) + '%';
      // Start just before the flagged part so its onset — and, in skip mode,
      // the jump over it — is visible rather than already past.
      v.currentTime = Math.max(0, cutStart - (skip ? 3 : 2));
    }} else {{
      v.currentTime = Math.min(pad, v.duration / 3);
    }}
  }};
  v.ontimeupdate = () => {{
    if (!showBar) return;
    const dur = v.duration || 1;
    ph.style.left = (100 * Math.min(v.currentTime, dur) / dur) + '%';
    if (skip && v.currentTime >= cutStart && v.currentTime < cutEnd - 0.05) {{
      v.currentTime = cutEnd;                 // jump the cut, as playback will
      tl.classList.add('flash');
      setTimeout(() => {{ if (tl) tl.classList.remove('flash'); }}, 500);
    }}
  }};
  v.onerror = () => box.innerHTML = '<div class=play><span>clip failed</span></div>';
}}

function card(s) {{
  const el = document.createElement('div');
  const tentative = TENTATIVE.includes(s.category);
  el.className = 'cell' + (tentative ? ' tentative' : '')
    + (s.approved === true ? ' approved' : s.approved === false ? ' rejected' : '');
  const tm = timing(s), w = word(s);
  const canMute = s.recommendedAction === 'mute';
  const canVoice = s.recommendedAction === 'voice';
  // Each finding is set to how it should be acted on. skip works live in
  // Jellyfin; mute/voice/blur only take effect in a rendered clean copy.
  // "voice" removes only the vocals (Demucs) so music/ambient play through.
  const actLabels = {{mute:'mute (silence all)', voice:'voice-only mute', blur:'blur', skip:'skip'}};
  const acts = ['mute','voice','blur','skip'].map(a =>
    `<option value=${{a}} ${{s.recommendedAction===a?'selected':''}}>${{actLabels[a]}}</option>`).join('');
  el.innerHTML = `
    <div class=shot>
      <img loading=lazy src="/api/thumbnail?path=${{encodeURIComponent(MEDIA)}}&ms=${{Math.floor((s.startMs+s.endMs)/2)}}">
      <div class=play><span>&#9654; Play clip</span></div>
    </div>
    <div class=clipbtns>
      <button class=play-plain>&#9654; Play scene</button>
      <button class=play-skip title="Play the scene as it will play once this span is skipped">&#9197; Preview skip</button>
      ${{canMute ? '<button class=play-muted>&#9654; Play muted</button>' : ''}}
      ${{canVoice ? '<button class=play-voice>&#9654; Play voice-removed</button>' : ''}}
      <button class=edit-timing>&#9998; Timing</button>
      <button class=dup title="Copy this finding; then retime the copy to another place">&#10697; Duplicate</button>
      <button class=del title="Delete this finding">&#10005; Delete</button>
    </div>
    <div class=meta>
      <label class=mergepick title="Select to merge with others"><input type=checkbox class=mergesel> merge</label>
      <b>#${{s.id}}</b> ${{fmtHMS(s.startMs)}} – ${{fmtHMS(s.endMs)}}
      (${{((s.endMs-s.startMs)/1000).toFixed(1)}}s)${{
        tm ? `<span class="tag ${{tm.exact?'exact':'est'}}">${{tm.label}}</span>` : ''}}<br>
      <span class=cat>${{s.category}}</span>${{w ? ` <span class=word>“${{w}}”</span>` : ''}}${{
        tentative ? '<span class=tag>needs your call</span>' : ''}} ${{s.confidence}} · ${{s.engine}}<br>
      <span class=why>${{(s.reasoning||'').replace(/</g,'&lt;')}}</span>
    </div>
    <div class=btns>
      <button class="yes ${{s.approved===true?'on':''}}">Bad — act on it</button>
      <button class="no ${{s.approved===false?'on':''}}">Fine — ignore</button>
      <select class=actsel title="What to do with this finding when acted on">${{acts}}</select>
    </div>
    <div class=editor></div>`;
  const shot = el.querySelector('.shot');
  shot.onclick = () => playClip(shot, s, false);
  el.querySelector('.play-plain').onclick = () => playClip(shot, s, false);
  el.querySelector('.play-skip').onclick = () => playClip(shot, s, false, false, null, true);
  const pm = el.querySelector('.play-muted');
  if (pm) pm.onclick = () => playClip(shot, s, true, false);
  const pv = el.querySelector('.play-voice');
  if (pv) pv.onclick = () => playClip(shot, s, false, true);

  const [yes, no] = el.querySelectorAll('.btns button');
  const send = v => fetch(`/api/segments/${{s.id}}?path=${{encodeURIComponent(MEDIA)}}`, {{
      method: 'PATCH', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{approved: v}})
    }}).then(() => {{ s.approved = v; el.replaceWith(card(s)); apply(); tally(); }});
  yes.onclick = () => send(s.approved === true ? null : true);
  no.onclick  = () => send(s.approved === false ? null : false);

  // Change what the finding does (mute/blur/skip). Re-render so "Play muted"
  // and the action badge update to match.
  const sel = el.querySelector('.actsel');
  sel.onchange = () => fetch(`/api/segments/${{s.id}}?path=${{encodeURIComponent(MEDIA)}}`, {{
      method: 'PATCH', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{recommendedAction: sel.value}})
    }}).then(() => {{ s.recommendedAction = sel.value; el.replaceWith(card(s)); apply(); tally(); }});

  // Tick to include this finding in a merge. The card highlights while picked.
  const mp = el.querySelector('.mergesel');
  mp.checked = selected.has(s.id);
  el.classList.toggle('picked', mp.checked);
  mp.onchange = () => {{
    if (mp.checked) selected.add(s.id); else selected.delete(s.id);
    el.classList.toggle('picked', mp.checked);
    updateMergeBtn();
  }};

  el.querySelector('.edit-timing').onclick = () => toggleTiming(el, s, el.querySelector('.editor'));

  // The description is editable in place — e.g. to fix the stale shot reference
  // a duplicated finding carries, or to annotate a manual one. Saves on blur if
  // it changed (Ctrl/Cmd+Enter commits without leaving the field).
  const why = el.querySelector('.why');
  why.contentEditable = 'true';
  why.spellcheck = false;
  why.title = 'Click to edit the description';
  why.addEventListener('keydown', e => {{
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{ e.preventDefault(); why.blur(); }}
  }});
  why.addEventListener('blur', () => {{
    const val = why.textContent;
    if (val === (s.reasoning || '')) return;  // unchanged
    fetch(`/api/segments/${{s.id}}?path=${{encodeURIComponent(MEDIA)}}`, {{
      method: 'PATCH', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{reasoning: val}})
    }}).then(r => {{ if (r.ok) s.reasoning = val; else why.textContent = s.reasoning || ''; }})
      .catch(() => {{ why.textContent = s.reasoning || ''; }});
  }});

  // Duplicate: create a manual copy at the same spot, then reload so the new
  // card appears. The reviewer retimes the copy (editable times) to its place.
  el.querySelector('.dup').onclick = () => {{
    fetch(`/api/segments?path=${{encodeURIComponent(MEDIA)}}`, {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{startMs: s.startMs, endMs: s.endMs, category: s.category,
        recommendedAction: s.recommendedAction, reasoning: s.reasoning || ''}})
    }}).then(r => {{ if (!r.ok) throw new Error('duplicate failed'); return r.json(); }})
      .then(() => location.reload()).catch(() => {{}});
  }};

  // Delete: remove the finding outright (we can add them now, so we must be
  // able to remove them). Confirmed, since it can't be undone.
  el.querySelector('.del').onclick = () => {{
    if (!confirm('Delete finding #' + s.id + '? This cannot be undone.')) return;
    fetch(`/api/segments/${{s.id}}?path=${{encodeURIComponent(MEDIA)}}`, {{ method: 'DELETE' }})
      .then(r => {{ if (!r.ok) throw new Error('delete failed'); return r.json(); }})
      .then(() => location.reload()).catch(() => {{}});
  }};
  return el;
}}

const fmtMs = ms => {{ const s = Math.floor(ms / 1000);
  return `${{Math.floor(s / 60)}}:${{String(s % 60).padStart(2, '0')}}`
       + `.${{String(Math.floor(ms % 1000)).padStart(3, '0')}}`; }};

// H:MM:SS.mmm, for the editable time fields, so hours-in are readable.
const fmtHMS = ms => {{ const t = Math.floor(ms / 1000);
  return `${{Math.floor(t / 3600)}}:${{String(Math.floor(t % 3600 / 60)).padStart(2, '0')}}`
       + `:${{String(t % 60).padStart(2, '0')}}.${{String(Math.floor(ms % 1000)).padStart(3, '0')}}`; }};

// Flexible parse for a typed time: H:MM:SS.mmm, MM:SS(.mmm), or plain seconds.
// Returns ms (>= 0) or null if it can't be read.
function parseTime(str) {{
  str = String(str == null ? '' : str).trim();
  if (!str) return null;
  const p = str.split(':').map(x => parseFloat(x));
  if (!p.length || p.some(x => !isFinite(x))) return null;
  let sec;
  if (p.length === 3) sec = p[0] * 3600 + p[1] * 60 + p[2];
  else if (p.length === 2) sec = p[0] * 60 + p[1];
  else sec = p[0];
  return Math.max(0, Math.round(sec * 1000));
}}

// Waveform timing editor. Zoomed to ~160px/s so 25ms is a few pixels; the
// scrollable window spans the finding ±PAD s, so a mute can be dragged onto
// the exact word by eye (and ear, via Preview muted) and saved to the sidecar.
// Visual findings (scene detections) get a filmstrip to place by eye; audio
// findings (spoken words) get a waveform to place by eye and ear. Check the
// category as well as the engine: a duplicated or merged finding carries the
// 'manual' engine but keeps its scene category, so keying off the engine alone
// wrongly opened a waveform for a copied visual finding. Categories mirror
// worker/policy.py.
const VISUAL_CATS = ['nudity', 'sexual_activity', 'intense_kissing', 'suggestive'];
function isVisual(s) {{
  return s.engine === 'vlm' || s.engine === 'pureframe' || VISUAL_CATS.includes(s.category);
}}

function toggleTiming(cell, s, box) {{
  if (box.dataset.open === '1') {{
    box.dataset.open = ''; box.classList.remove('open'); box.innerHTML = ''; return;
  }}
  box.dataset.open = '1'; box.classList.add('open');
  openTimingAt(cell, s, box, s.startMs, s.endMs);
}}

// (Re)load the editor's frames/waveform for a window centred on [atStart,atEnd]:
// the finding's saved spot on first open, or the edited bounds when the reviewer
// moves the finding somewhere the current window doesn't reach (a duplicate
// dragged minutes away). Without re-anchoring, the strip stayed at the old spot
// and a far-moved finding showed a blank/mismatched strip.
function openTimingAt(cell, s, box, atStart, atEnd) {{
  if (isVisual(s)) {{
    box.innerHTML = '<div class=tload>loading frames&hellip;</div>';
    buildTiming(cell, s, box, {{visual: true}}, atStart, atEnd);
  }} else {{
    box.innerHTML = '<div class=tload>loading waveform&hellip;</div>';
    fetch(`/api/peaks?path=${{encodeURIComponent(MEDIA)}}&startMs=${{atStart}}&endMs=${{atEnd}}&pad=${{PAD}}`)
      .then(r => r.json())
      .then(d => buildTiming(cell, s, box, {{peaks: d.peaks}}, atStart, atEnd))
      .catch(() => box.innerHTML = '<div class=tload>could not load waveform</div>');
  }}
}}

function buildTiming(cell, s, box, mode, atStart, atEnd) {{
  // Window bounds match the server (build_peaks/build_filmstrip): the anchor
  // span ±PAD, clamped at the file start. The anchor is the finding's saved
  // spot on open, or its edited bounds after a recenter — so the strip always
  // shows where the finding currently is.
  const winStart = Math.max(0, atStart - PAD * 1000), winEnd = atEnd + PAD * 1000;
  const span = Math.max(1, winEnd - winStart);
  const PXPS = 160, SNAP = 25, H = 90;
  const W = Math.max(320, Math.round(span / 1000 * PXPS));
  const clamp = t => Math.max(winStart, Math.min(winEnd, t));
  let st = clamp(atStart), en = clamp(atEnd);
  const canMute = s.recommendedAction === 'mute';
  const canVoice = s.recommendedAction === 'voice';
  const bg = mode.visual
    ? `<img class=strip style="width:${{W}}px;height:${{H}}px" `
      + `src="/api/filmstrip?path=${{encodeURIComponent(MEDIA)}}`
      + `&startMs=${{atStart}}&endMs=${{atEnd}}&pad=${{PAD}}">`
    : `<canvas width=${{W}} height=${{H}}></canvas>`;

  box.innerHTML = `
    <div class=wavewrap>
      <div class=wavebox style="width:${{W}}px;height:${{H}}px">
        ${{bg}}
        <div class=region></div>
        <div class=selregion></div>
        <div class="handle start"><div class=grip></div></div>
        <div class="handle end"><div class=grip></div></div>
      </div>
    </div>
    <div class=tctrls>
      <span class=tlabel>Start</span>
      <button data-h=start data-d=-1000>&#9664;&#9664; 1s</button>
      <button data-h=start data-d=-25>&#9664; 25ms</button>
      <button data-h=start data-d=25>25ms &#9654;</button>
      <button data-h=start data-d=1000>1s &#9654;&#9654;</button>
      <input class=tinput id=tstart-${{s.id}} size=11 title="type any time: H:MM:SS.mmm">
    </div>
    <div class=tctrls>
      <span class=tlabel>End</span>
      <button data-h=end data-d=-1000>&#9664;&#9664; 1s</button>
      <button data-h=end data-d=-25>&#9664; 25ms</button>
      <button data-h=end data-d=25>25ms &#9654;</button>
      <button data-h=end data-d=1000>1s &#9654;&#9654;</button>
      <input class=tinput id=tend-${{s.id}} size=11 title="type any time: H:MM:SS.mmm">
      <span style="margin-left:14px" class=tread id=tdur-${{s.id}}></span>
    </div>
    <div class=tctrls>
      <button class=tprev>&#9654; ${{canMute ? 'Preview muted' : canVoice ? 'Preview voice-removed' : 'Preview scene'}}</button>
      <button class=trecenter title="Reload the frames/waveform around the current start and end">&#8635; Frames here</button>
      <button class=tsave>Save timing</button>
      <button class=tcancel>Cancel</button>
      <span class=selread id=tsel-${{s.id}}></span>
    </div>
    <div class=tctrls>
      <span class=thint>drag the handles or type a time to set the bounds; drag across the
      strip to select a span — it shows its time and can be used as the bounds</span>
    </div>`;

  const wrap = box.querySelector('.wavewrap');
  const wbox = box.querySelector('.wavebox');
  const canvas = box.querySelector('canvas');
  const hStart = box.querySelector('.handle.start');
  const hEnd = box.querySelector('.handle.end');
  const region = box.querySelector('.region');
  const rStart = box.querySelector('#tstart-' + s.id);
  const rEnd = box.querySelector('#tend-' + s.id);
  const rDur = box.querySelector('#tdur-' + s.id);

  const xOf = t => (t - winStart) / span * W;
  const tOf = x => Math.round((winStart + x / W * span) / SNAP) * SNAP;

  // Waveform: one bar per peak (audio findings only; visual show the filmstrip).
  if (canvas && mode.peaks) {{
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#3a4650';
    const n = mode.peaks.length, bw = W / n;
    for (let i = 0; i < n; i++) {{
      const h = mode.peaks[i] * (H - 8);
      ctx.fillRect(i * bw, (H - h) / 2, Math.max(1, bw - 0.5), h);
    }}
  }}

  // A typed time can fall outside the drawn window, so clamp only the handle's
  // pixel position — the value stays whatever was entered and is what gets saved.
  const cx = t => Math.max(0, Math.min(W, xOf(t)));
  function upd() {{
    hStart.style.left = cx(st) + 'px';
    hEnd.style.left = cx(en) + 'px';
    region.style.left = cx(st) + 'px';
    region.style.width = Math.max(0, cx(en) - cx(st)) + 'px';
    // Don't overwrite the field being typed in.
    if (document.activeElement !== rStart) rStart.value = fmtHMS(st);
    if (document.activeElement !== rEnd) rEnd.value = fmtHMS(en);
    rDur.textContent = (en - st) + 'ms';
  }}

  // Type any time into the fields (H:MM:SS.mmm). Unlike drag/nudge, this is not
  // clamped to the window — so a duplicated or added finding can be moved
  // anywhere. Preview and Save use these values.
  rStart.addEventListener('change', () => {{
    const v = parseTime(rStart.value); if (v !== null) st = v; upd();
  }});
  rEnd.addEventListener('change', () => {{
    const v = parseTime(rEnd.value); if (v !== null) en = v; upd();
  }});
  upd();
  // Centre the finding in the scroll view.
  wrap.scrollLeft = xOf((st + en) / 2) - wrap.clientWidth / 2;

  function drag(handle, isStart) {{
    handle.querySelector('.grip').addEventListener('mousedown', e => {{
      e.preventDefault();
      const move = ev => {{
        const x = ev.clientX - wbox.getBoundingClientRect().left;
        const t = Math.max(winStart, Math.min(winEnd, tOf(x)));
        if (isStart) st = Math.min(t, en - SNAP); else en = Math.max(t, st + SNAP);
        upd();
      }};
      const up = () => {{ document.removeEventListener('mousemove', move);
                          document.removeEventListener('mouseup', up); }};
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    }});
  }}
  drag(hStart, true); drag(hEnd, false);

  // Audition a spot: drag across the waveform/filmstrip background to select a
  // region and play just that on release, so you can hear/see what is there
  // before moving the handles. The segment's own start/end bars stay put — this
  // selection is a separate scratch overlay, not the saved bounds.
  const selEl = box.querySelector('.selregion');
  const selRead = box.querySelector('#tsel-' + s.id);
  let selA = null, selB = null;
  function drawSel() {{
    if (selA === null || selB === null) {{
      selEl.style.display = 'none';
      if (selRead) selRead.textContent = '';
      return;
    }}
    const a = Math.min(selA, selB), b = Math.max(selA, selB);
    // Explicit 'block': the class sets display:none, so '' would revert to that.
    selEl.style.display = 'block';
    selEl.style.left = cx(a) + 'px';
    selEl.style.width = Math.max(0, cx(b) - cx(a)) + 'px';
    // Live time of the selection, so a reviewer can read the exact spot they
    // watched and set the finding's bounds to it.
    if (selRead) {{
      selRead.textContent = 'selection ' + fmtHMS(a) + ' – ' + fmtHMS(b)
        + ' (' + ((b - a) / 1000).toFixed(2) + 's)';
    }}
  }}
  wbox.addEventListener('mousedown', e => {{
    if (e.target.closest('.handle')) return;  // let handle drags win
    e.preventDefault();
    const originX = wbox.getBoundingClientRect().left;
    selA = Math.max(winStart, Math.min(winEnd, tOf(e.clientX - originX)));
    selB = selA;
    drawSel();
    const move = ev => {{
      selB = Math.max(winStart, Math.min(winEnd, tOf(ev.clientX - originX)));
      drawSel();
    }};
    const up = () => {{
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      const a = Math.min(selA, selB), b = Math.max(selA, selB);
      if (b - a >= 100) {{  // a real drag (>=100ms), not a stray click
        playClip(cell.querySelector('.shot'), {{startMs: a, endMs: b}}, false, false, 0.15);
        // Offer the selection as the finding's bounds — one click sets both.
        if (selRead) {{
          selRead.innerHTML = 'selection <b>' + fmtHMS(a) + '</b> – <b>' + fmtHMS(b)
            + '</b> (' + ((b - a) / 1000).toFixed(2) + 's)';
          const use = document.createElement('button');
          use.className = 'seluse';
          use.textContent = 'Use as bounds';
          use.onclick = () => {{ st = a; en = b; upd(); selA = selB = null; drawSel(); }};
          selRead.appendChild(use);
        }}
      }} else {{
        selA = selB = null; drawSel();  // a click clears any selection
      }}
    }};
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }});

  box.querySelectorAll('.tctrls [data-h]').forEach(b => b.onclick = () => {{
    const d2 = parseInt(b.dataset.d, 10);
    if (b.dataset.h === 'start') st = clamp(Math.min(st + d2, en - SNAP));
    else en = clamp(Math.max(en + d2, st + SNAP));
    upd();
  }});

  box.querySelector('.tprev').onclick = () =>
    playClip(cell.querySelector('.shot'), {{startMs: st, endMs: en}}, canMute, canVoice);

  // Re-anchor the strip around the current bounds — for when a finding has been
  // typed/nudged to a spot the drawn window doesn't cover.
  box.querySelector('.trecenter').onclick = () => openTimingAt(cell, s, box, st, en);

  box.querySelector('.tcancel').onclick = () => toggleTiming(cell, s, box);

  box.querySelector('.tsave').onclick = () => {{
    fetch(`/api/segments/${{s.id}}?path=${{encodeURIComponent(MEDIA)}}`, {{
      method: 'PATCH', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{startMs: st, endMs: en}})
    }}).then(() => {{
      // Did the finding move out of the window we were showing? Only then is a
      // reset worth the reload — a small in-window nudge shouldn't flash.
      const moved = st < winStart || en > winEnd || en < winStart || st > winEnd;
      s.startMs = st; s.endMs = en;
      // Rebuild the card (updates the displayed times/badges); if it moved, reopen
      // the editor anchored on the NEW bounds so the strip and preview reset to
      // 15s either side of where the finding now is, not where it was.
      const fresh = card(s);
      cell.replaceWith(fresh); apply(); tally();
      if (moved) fresh.querySelector('.edit-timing').click();
    }});
  }};
}}

function visible(s) {{
  if (typeFilter !== 'all' && typeOf(s) !== typeFilter) return false;
  if (filter === 'undecided') return s.approved === null || s.approved === undefined;
  if (filter === 'approved') return s.approved === true;
  if (filter === 'rejected') return s.approved === false;
  return true;
}}
function apply() {{
  [...grid.children].forEach((el, i) =>
    el.classList.toggle('hidden', !visible(segs[i])));
}}
function tally() {{
  const a = segs.filter(s => s.approved === true).length;
  const r = segs.filter(s => s.approved === false).length;
  document.getElementById('summary').textContent =
    `${{segs.length}} finding(s) — ${{a}} approved, ${{r}} rejected, ${{segs.length-a-r}} undecided`;
  const shown = segs.filter(visible).length;
  document.getElementById('shownCount').textContent = shown;
  document.querySelectorAll('#bulk button').forEach(b => b.disabled = shown === 0);
}}

const grid = document.getElementById('grid');
function renderGrid() {{
  grid.innerHTML = '';
  segs.forEach(s => grid.appendChild(card(s)));
  apply(); tally();
}}

// One chip per type present, most-common first, each carrying its own count.
function buildTypeFilters() {{
  const counts = {{}};
  segs.forEach(s => {{ const t = typeOf(s); counts[t] = (counts[t] || 0) + 1; }});
  const row = document.getElementById('typeFilters');
  const order = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  const chip = (key, label, n) => {{
    const b = document.createElement('button');
    b.dataset.t = key;
    b.classList.toggle('on', key === typeFilter);
    b.innerHTML = `${{label}} <span class=count>${{n}}</span>`;
    b.onclick = () => {{
      typeFilter = key;
      row.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
      apply(); tally();
    }};
    return b;
  }};
  row.appendChild(chip('all', 'All types', segs.length));
  order.forEach(t => row.appendChild(chip(t, t.replace(/</g, '&lt;'), counts[t])));
}}

document.querySelectorAll('.filters button[data-f]').forEach(b => b.onclick = () => {{
  filter = b.dataset.f;
  document.querySelectorAll('.filters button[data-f]').forEach(x => x.classList.toggle('on', x===b));
  apply(); tally();
}});

// Bulk decision on exactly the findings currently shown — so a reviewer who
// filtered to one word can settle the whole group at once. One request, one
// sidecar write; the server echoes the timeline back and the grid re-renders.
function bulkSet(v) {{
  const ids = segs.filter(visible).map(s => s.id);
  if (!ids.length) return;
  fetch(`/api/segments?path=${{encodeURIComponent(MEDIA)}}`, {{
    method: 'PATCH', headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ids, approved: v}})
  }}).then(r => r.json()).then(tl => {{
    const byId = {{}}; tl.segments.forEach(x => byId[x.id] = x);
    segs.forEach(s => {{ if (byId[s.id]) s.approved = byId[s.id].approved; }});
    renderGrid();
  }});
}}
document.querySelector('#bulk .set-yes').onclick   = () => bulkSet(true);
document.querySelector('#bulk .set-no').onclick    = () => bulkSet(false);
document.querySelector('#bulk .set-clear').onclick = () => bulkSet(null);

// Merge the ticked findings into one segment spanning the earliest start to
// the latest end. The originals are replaced by a single approved skip (or the
// chosen action), so a scene flagged shot by shot becomes one clean skip. The
// timeline changes shape (ids, counts), so reload rather than patch in place.
function updateMergeBtn() {{
  const n = selected.size;
  const b = document.getElementById('mergeBtn');
  b.textContent = 'Merge selected (' + n + ')';
  b.disabled = n < 2;
}}
function doMerge() {{
  const ids = [...selected];
  if (ids.length < 2) return;
  const action = document.getElementById('mergeAction').value;
  const b = document.getElementById('mergeBtn');
  b.disabled = true; b.textContent = 'Merging&hellip;';
  fetch(`/api/segments/merge?path=${{encodeURIComponent(MEDIA)}}`, {{
    method: 'POST', headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ids, recommendedAction: action, approved: true}})
  }}).then(r => {{ if (!r.ok) throw new Error('merge failed'); return r.json(); }})
    .then(() => location.reload())
    .catch(() => {{ updateMergeBtn(); }});
}}
document.getElementById('mergeBtn').onclick = doMerge;

// Add a finding by hand: type a start and end (any time) and it's created as an
// approved manual segment. Reloading shows it, and its Play clip loads the video
// from that time. Duplicate (per card) uses the same create path.
const addForm = document.getElementById('addform');
document.getElementById('addToggle').onclick = () => {{
  addForm.style.display = addForm.style.display === 'none' ? '' : 'none';
}};
document.getElementById('addCancel').onclick = () => {{ addForm.style.display = 'none'; }};
document.getElementById('addSave').onclick = () => {{
  const a = parseTime(document.getElementById('addStart').value);
  const b = parseTime(document.getElementById('addEnd').value);
  if (a === null || b === null) {{ alert('Enter start and end times as H:MM:SS.mmm'); return; }}
  const st = Math.min(a, b), en = Math.max(a, b);
  const btn = document.getElementById('addSave');
  btn.disabled = true;
  fetch(`/api/segments?path=${{encodeURIComponent(MEDIA)}}`, {{
    method: 'POST', headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{startMs: st, endMs: en, category: 'manual',
      recommendedAction: document.getElementById('addAction').value,
      reasoning: document.getElementById('addNote').value}})
  }}).then(r => {{ if (!r.ok) throw new Error('add failed'); return r.json(); }})
    .then(() => location.reload())
    .catch(() => {{ btn.disabled = false; alert('Could not add the segment.'); }});
}};

buildTypeFilters();
renderGrid();
</script>
"""


def render_page(media: Path, timeline: Timeline) -> str:
    return PAGE.format(
        title=media.stem,
        count=len(timeline.segments),
        media=str(media),
        pad=CLIP_PAD_S,
        segments=json.dumps([s.model_dump() for s in timeline.segments]),
    )
