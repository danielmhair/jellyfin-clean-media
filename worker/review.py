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


def clip_path(media: Path, start_ms: int, end_ms: int, pad_s: float, mute: bool) -> Path:
    """Cache location for a review clip; regenerating one is a few seconds."""
    key = hashlib.sha256(
        f"{media}|{start_ms}|{end_ms}|{pad_s}|{mute}".encode("utf-8")
    ).hexdigest()[:20]
    cache = Path(tempfile.gettempdir()) / "cleanmedia-clips"
    cache.mkdir(exist_ok=True)
    return cache / f"{key}.mp4"


def build_clip(
    media: Path, start_ms: int, end_ms: int, pad_s: float = 15.0, mute: bool = False
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
    """
    out = clip_path(media, start_ms, end_ms, pad_s, mute)
    if out.is_file() and out.stat().st_size > 0:
        return out

    start = max(0.0, start_ms / 1000 - pad_s)
    duration = (end_ms - start_ms) / 1000 + 2 * pad_s

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
 .hint{{color:#8b949e;font-size:11px;padding:0 12px 8px}}
 .meta{{padding:10px 12px;font-size:13px;line-height:1.55}}
 .cat{{color:#ff8f6b;font-weight:600}} .act{{color:#7ee787}}
 .word{{color:#ffd479;font-weight:600}}
 .why{{color:#9aa;font-style:italic}}
 .clipbtns{{display:flex;gap:8px;padding:0 12px 8px}}
 .clipbtns button{{background:#22262c;font-size:12px;padding:7px}}
 .btns{{display:flex;gap:8px;padding:0 12px 12px}}
 button{{flex:1;padding:9px;border:0;border-radius:6px;font-size:13px;
         font-weight:600;cursor:pointer;background:#30363d;color:#eee}}
 button.yes.on{{background:#238636}} button.no.on{{background:#da3633}}
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

function playClip(box, s, mute) {{
  box.innerHTML = '<div class=play><span>loading clip&hellip;</span></div>';
  const tight = document.getElementById('tight').checked;
  const pad = tight ? 2 : PAD;
  const v = document.createElement('video');
  v.controls = true; v.autoplay = true; v.preload = 'auto';
  v.src = `/api/clip?path=${{encodeURIComponent(MEDIA)}}`
        + `&startMs=${{s.startMs}}&endMs=${{s.endMs}}&pad=${{pad}}`
        + (mute ? '&mute=true' : '');
  // The clip is padded, so jump to where the flagged part actually begins.
  v.onloadedmetadata = () => {{ box.innerHTML = ''; box.appendChild(v);
                                v.currentTime = Math.min(pad, v.duration / 3); }};
  v.onerror = () => box.innerHTML = '<div class=play><span>clip failed</span></div>';
}}

function card(s) {{
  const el = document.createElement('div');
  const tentative = TENTATIVE.includes(s.category);
  el.className = 'cell' + (tentative ? ' tentative' : '')
    + (s.approved === true ? ' approved' : s.approved === false ? ' rejected' : '');
  const tm = timing(s), w = word(s);
  const canMute = s.recommendedAction === 'mute';
  el.innerHTML = `
    <div class=shot>
      <img loading=lazy src="/api/thumbnail?path=${{encodeURIComponent(MEDIA)}}&ms=${{Math.floor((s.startMs+s.endMs)/2)}}">
      <div class=play><span>&#9654; Play clip</span></div>
    </div>
    <div class=clipbtns>
      <button class=play-plain>&#9654; Play scene</button>
      ${{canMute ? '<button class=play-muted>&#9654; Play muted</button>' : ''}}
    </div>
    <div class=meta>
      <b>#${{s.id}}</b> ${{fmt(s.startMs)}} – ${{fmt(s.endMs)}}
      (${{((s.endMs-s.startMs)/1000).toFixed(1)}}s)${{
        tm ? `<span class="tag ${{tm.exact?'exact':'est'}}">${{tm.label}}</span>` : ''}}<br>
      <span class=cat>${{s.category}}</span>${{w ? ` <span class=word>“${{w}}”</span>` : ''}}${{
        tentative ? '<span class=tag>needs your call</span>' : ''}} ${{s.confidence}} ·
      <span class=act>${{s.recommendedAction}}</span> · ${{s.engine}}<br>
      <span class=why>${{(s.reasoning||'').replace(/</g,'&lt;')}}</span>
    </div>
    <div class=btns>
      <button class="yes ${{s.approved===true?'on':''}}">Bad — act on it</button>
      <button class="no ${{s.approved===false?'on':''}}">Fine — ignore</button>
    </div>`;
  const shot = el.querySelector('.shot');
  shot.onclick = () => playClip(shot, s, false);
  el.querySelector('.play-plain').onclick = () => playClip(shot, s, false);
  const pm = el.querySelector('.play-muted');
  if (pm) pm.onclick = () => playClip(shot, s, true);

  const [yes, no] = el.querySelectorAll('.btns button');
  const send = v => fetch(`/api/segments/${{s.id}}?path=${{encodeURIComponent(MEDIA)}}`, {{
      method: 'PATCH', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{approved: v}})
    }}).then(() => {{ s.approved = v; el.replaceWith(card(s)); apply(); tally(); }});
  yes.onclick = () => send(s.approved === true ? null : true);
  no.onclick  = () => send(s.approved === false ? null : false);
  return el;
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
