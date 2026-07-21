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
    return [r for r in roots if r.is_dir()]


def resolve_media(path: str) -> Optional[Path]:
    """Map a caller's path onto a local file.

    Jellyfin knows a film as /volume1/Media/Movies/Film.mkv while the
    worker has C:/media/Film.mkv — the same movie, different mount. Rather
    than make administrators maintain a path-mapping table, fall back to
    matching the file name inside the configured media roots.
    """
    candidate = Path(path)
    if candidate.is_file():
        return candidate

    wanted = PurePosixPath(path.replace("\\", "/")).name.lower()
    if not wanted:
        return None
    for root in media_roots():
        for found in root.rglob("*"):
            if found.is_file() and found.name.lower() == wanted:
                return found
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


def clip_path(media: Path, start_ms: int, end_ms: int, pad_s: float) -> Path:
    """Cache location for a review clip; regenerating one is a few seconds."""
    key = hashlib.sha256(
        f"{media}|{start_ms}|{end_ms}|{pad_s}".encode("utf-8")
    ).hexdigest()[:20]
    cache = Path(tempfile.gettempdir()) / "cleanmedia-clips"
    cache.mkdir(exist_ok=True)
    return cache / f"{key}.mp4"


def build_clip(
    media: Path, start_ms: int, end_ms: int, pad_s: float = 15.0
) -> Optional[Path]:
    """Extract the flagged span plus padding, transcoded for the browser.

    DVD sources are MPEG-2, which browsers will not play, so this always
    re-encodes. Clips are short and cached, so the cost is paid once per
    finding no matter how often it is replayed.
    """
    out = clip_path(media, start_ms, end_ms, pad_s)
    if out.is_file() and out.stat().st_size > 0:
        return out

    start = max(0.0, start_ms / 1000 - pad_s)
    duration = (end_ms - start_ms) / 1000 + 2 * pad_s
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(media), "-t", f"{duration:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "scale=640:-2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-c:a", "aac", "-b:a", "128k",
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
 .sub{{color:#9aa;margin-bottom:20px}}
 /* flex-start, or a tall card stretches every neighbour in its row */
 .grid{{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}}
 .cell{{width:440px;background:#1d1d1f;border-radius:10px;overflow:hidden;
        border:2px solid transparent}}
 .cell.approved{{border-color:#3fb950}} .cell.rejected{{border-color:#f85149;opacity:.55}}
 .cell.tentative{{background:#20201a}}
 .tag{{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
       font-weight:700;background:#7a5c00;color:#ffd479;margin-left:6px}}
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
 .why{{color:#9aa;font-style:italic}}
 .btns{{display:flex;gap:8px;padding:0 12px 12px}}
 button{{flex:1;padding:9px;border:0;border-radius:6px;font-size:13px;
         font-weight:600;cursor:pointer;background:#30363d;color:#eee}}
 button.yes.on{{background:#238636}} button.no.on{{background:#da3633}}
 .bar{{position:sticky;top:0;background:#111;padding:10px 0;margin-bottom:10px;z-index:9}}
</style>
<div class=bar>
  <h1>{title}</h1>
  <div class=sub id=summary>{count} finding(s) — approve what should be acted on</div>
</div>
<div class=grid id=grid></div>
<script>
const MEDIA = {media!r};
const segs = {segments};

// A weaker signal the model was told to raise rather than force a guess on.
const TENTATIVE = ['suggestive'];

function card(s) {{
  const el = document.createElement('div');
  const tentative = TENTATIVE.includes(s.category);
  el.className = 'cell' + (tentative ? ' tentative' : '')
    + (s.approved === true ? ' approved' : s.approved === false ? ' rejected' : '');
  const t = ms => {{ const x = ms/1000;
    return `${{Math.floor(x/3600)}}:${{String(Math.floor(x%3600/60)).padStart(2,'0')}}:${{(x%60).toFixed(1).padStart(4,'0')}}`; }};
  el.innerHTML = `
    <div class=shot>
      <img loading=lazy src="/api/thumbnail?path=${{encodeURIComponent(MEDIA)}}&ms=${{Math.floor((s.startMs+s.endMs)/2)}}">
      <div class=play><span>&#9654; Play clip</span></div>
    </div>
    <div class=hint>starts {pad:.0f}s before the flagged part</div>
    <div class=meta>
      <b>#${{s.id}}</b> ${{t(s.startMs)}} – ${{t(s.endMs)}}
      (${{((s.endMs-s.startMs)/1000).toFixed(1)}}s)<br>
      <span class=cat>${{s.category}}</span>${{tentative ? '<span class=tag>needs your call</span>' : ''}} ${{s.confidence}} ·
      <span class=act>${{s.recommendedAction}}</span> · ${{s.engine}}<br>
      <span class=why>${{(s.reasoning||'').replace(/</g,'&lt;')}}</span>
    </div>
    <div class=btns>
      <button class="yes ${{s.approved===true?'on':''}}">Bad — act on it</button>
      <button class="no ${{s.approved===false?'on':''}}">Fine — ignore</button>
    </div>`;
  el.querySelector('.shot').onclick = ev => {{
    const box = ev.currentTarget;
    box.innerHTML = '<div class=play><span>loading clip&hellip;</span></div>';
    const v = document.createElement('video');
    v.controls = true; v.autoplay = true; v.preload = 'auto';
    v.src = `/api/clip?path=${{encodeURIComponent(MEDIA)}}`
          + `&startMs=${{s.startMs}}&endMs=${{s.endMs}}`;
    // The clip is padded, so jump to where the flagged part actually begins.
    v.onloadedmetadata = () => {{ box.innerHTML = ''; box.appendChild(v);
                                  v.currentTime = Math.min({pad:.0f}, v.duration / 3); }};
    v.onerror = () => box.innerHTML = '<div class=play><span>clip failed</span></div>';
  }};

  const [yes, no] = el.querySelectorAll('button');
  const send = v => fetch(`/api/segments/${{s.id}}?path=${{encodeURIComponent(MEDIA)}}`, {{
      method: 'PATCH', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{approved: v}})
    }}).then(() => {{ s.approved = v; el.replaceWith(card(s)); tally(); }});
  yes.onclick = () => send(s.approved === true ? null : true);
  no.onclick  = () => send(s.approved === false ? null : false);
  return el;
}}

function tally() {{
  const a = segs.filter(s => s.approved === true).length;
  const r = segs.filter(s => s.approved === false).length;
  document.getElementById('summary').textContent =
    `${{segs.length}} finding(s) — ${{a}} approved, ${{r}} rejected, ${{segs.length-a-r}} undecided`;
}}

const grid = document.getElementById('grid');
segs.forEach(s => grid.appendChild(card(s)));
tally();
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
