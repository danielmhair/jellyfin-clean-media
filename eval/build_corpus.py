"""Build a labeled test corpus from already-reviewed films.

Real detection accuracy can't be judged from reasoning about prompts — it
needs real content with known answers. This script builds that: it reads
the *approved* findings out of already-reviewed films' sidecars (the same
`.cleanmedia.json` format the worker itself reads — see worker/review.py's
`load_timeline`), cuts a short window around each one (some clean lead-in,
the flagged content itself, some clean lead-out), and concatenates all of
them into one compact test video plus a manifest recording exactly which
millisecond range in the combined file is the true-positive region and
which is padding that should NOT be flagged.

That gives a fast, reusable way to compare *any* candidate detection
approach (a different model, a different prompt strategy, Ollama vs. MLX vs.
a hand-rolled server) against real ground truth in minutes instead of
re-running a multi-hour pass over full films for every experiment — and the
padding on each clip means the corpus tests false positives, not just
recall: a candidate that flags the clean lead-in should score worse than
one that doesn't, even though both "found" the positive region.

Usage:
    uv run python eval/build_corpus.py \\
        "D:\\Movies\\Iron Man 3 (2013)\\Iron Man 3 (2013).mkv" \\
        "D:\\Movies\\Thor The Dark World (2013)\\Thor The Dark World (2013).mkv" \\
        --out eval/corpus/v1

Only approved findings are used — an approved decision is the closest thing
this project has to verified ground truth (see worker/review.py's own
docstring: "Nothing acts on a finding until it is approved"). Rejected and
undecided findings are skipped; they were not confirmed correct or
incorrect, so they'd be noise in a test set, not a label.

ffmpeg specifics inherited from worker/render.py's own hard-won lesson: cut
with -ss/-to plus a real re-encode (never select+setpts, which desyncs
telecined sources whose container frame rate doesn't match what the decoder
emits), and give every clip a clean, zero-based timestamp before
concatenating — the concat demuxer only stream-copies safely when every
input already shares the same codec/resolution/fps, which per-clip
re-encoding guarantees regardless of how different the source films are.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker.review import load_timeline, sidecar_for  # noqa: E402

# Re-encode every clip to this common shape before concatenating, so clips
# from films of different resolution/fps/codec can be stream-copy-joined
# safely afterward. Matches vlm_engine.FRAME_WIDTH's own reasoning (visual
# token count, not raw resolution, drives inference cost) with a bit more
# headroom than that 512px inference-time downscale, since the corpus is a
# shared base several candidates will draw from at different scales.
CLIP_WIDTH = 960
CLIP_FPS = 24


@dataclass
class ClipRecord:
    source_media: str
    source_category: str
    source_engine: str
    source_confidence: float
    # Where this clip's frames landed in the combined output file.
    combined_start_ms: int
    combined_end_ms: int
    # The sub-range within [combined_start_ms, combined_end_ms) that is the
    # actual approved finding — the padding before/after it is a *negative*
    # region: a candidate that flags it is a false positive, not a hit.
    positive_start_ms: int
    positive_end_ms: int
    clip_file: str


def _ffprobe_duration_s(media: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
        capture_output=True, text=True, check=True,
    )
    return float(proc.stdout.strip())


def _cut_clip(media: Path, start_s: float, end_s: float, out_path: Path) -> None:
    """Cut [start_s, end_s) and re-encode to the shared corpus shape.

    -ss before -i seeks fast (keyframe-nearest) then -to trims precisely
    after decode starts, matching the pattern worker/render.py/review.py
    already use elsewhere in this codebase for the same reason: a bare
    select+setpts filter is what desyncs telecined sources, not a plain
    trim+re-encode.
    """
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-i", str(media),
            "-vf", f"scale={CLIP_WIDTH}:-2,fps={CLIP_FPS},settb=1/1000",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "48000",
            str(out_path),
        ],
        check=True,
    )


def _clip_duration_s(clip_path: Path) -> float:
    return _ffprobe_duration_s(clip_path)


def build_corpus(
    media_paths: list[Path],
    out_dir: Path,
    pad_before_s: float,
    pad_after_s: float,
    max_clip_s: float,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(exist_ok=True)

    records: list[ClipRecord] = []
    concat_list: list[str] = []
    clip_index = 0
    skipped_no_sidecar = 0
    skipped_no_approved = 0

    for media in media_paths:
        if not media.is_file():
            print(f"  SKIP (not found): {media}", file=sys.stderr)
            continue
        timeline = load_timeline(media)
        if timeline is None:
            print(f"  SKIP (no sidecar, not analyzed): {sidecar_for(media)}", file=sys.stderr)
            skipped_no_sidecar += 1
            continue
        approved = [s for s in timeline.segments if s.approved is True]
        if not approved:
            print(f"  SKIP (no approved findings): {media.name}", file=sys.stderr)
            skipped_no_approved += 1
            continue

        duration_s = _ffprobe_duration_s(media)
        print(f"==> {media.name}: {len(approved)} approved finding(s)")

        for seg in approved:
            pos_start_s = seg.startMs / 1000.0
            pos_end_s = max(pos_start_s, seg.endMs / 1000.0)
            pos_len_s = pos_end_s - pos_start_s

            # Shrink padding first if the positive region alone is already
            # close to the cap (a long scene) rather than silently
            # truncating the flagged content itself, which would corrupt
            # the ground truth.
            budget_s = max(0.0, max_clip_s - pos_len_s)
            before_s = min(pad_before_s, budget_s / 2)
            after_s = min(pad_after_s, budget_s - before_s)

            clip_start_s = max(0.0, pos_start_s - before_s)
            clip_end_s = min(duration_s, pos_end_s + after_s)
            actual_before_s = pos_start_s - clip_start_s

            clip_file = clips_dir / f"clip_{clip_index:04d}.mp4"
            try:
                _cut_clip(media, clip_start_s, clip_end_s, clip_file)
            except subprocess.CalledProcessError as exc:
                print(f"  ffmpeg failed on {media.name} @ {pos_start_s:.1f}s: {exc}", file=sys.stderr)
                continue

            clip_dur_ms = int(round(_clip_duration_s(clip_file) * 1000))
            combined_start_ms = records[-1].combined_end_ms if records else 0
            positive_start_ms = combined_start_ms + int(round(actual_before_s * 1000))
            positive_end_ms = positive_start_ms + (seg.endMs - seg.startMs)

            records.append(ClipRecord(
                source_media=str(media),
                source_category=seg.category,
                source_engine=seg.engine,
                source_confidence=seg.confidence,
                combined_start_ms=combined_start_ms,
                combined_end_ms=combined_start_ms + clip_dur_ms,
                positive_start_ms=positive_start_ms,
                positive_end_ms=positive_end_ms,
                clip_file=str(clip_file.relative_to(out_dir)),
            ))
            concat_list.append(f"file '{clip_file.resolve().as_posix()}'")
            clip_index += 1

    if not records:
        print("Nothing to build — no approved findings found in any input film.", file=sys.stderr)
        sys.exit(1)

    concat_txt = out_dir / "concat_list.txt"
    concat_txt.write_text("\n".join(concat_list), encoding="utf-8")

    combined_path = out_dir / "combined.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(concat_txt), "-c", "copy", str(combined_path)],
        check=True,
    )

    manifest = {
        "clipWidthPx": CLIP_WIDTH,
        "clipFps": CLIP_FPS,
        "padBeforeS": pad_before_s,
        "padAfterS": pad_after_s,
        "maxClipS": max_clip_s,
        "sourceFilms": [str(m) for m in media_paths],
        "clips": [asdict(r) for r in records],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total_s = records[-1].combined_end_ms / 1000.0
    print(f"\nBuilt {len(records)} clip(s), {total_s / 60:.1f} minute(s) total.")
    print(f"  video    : {combined_path}")
    print(f"  manifest : {out_dir / 'manifest.json'}")
    if skipped_no_sidecar or skipped_no_approved:
        print(f"  skipped  : {skipped_no_sidecar} unanalyzed, {skipped_no_approved} with no approved findings")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media", nargs="+", type=Path, help="already-reviewed film(s) to pull findings from")
    ap.add_argument("--out", type=Path, default=Path("eval/corpus/v1"), help="output directory")
    ap.add_argument("--pad-before", type=float, default=3.0, help="seconds of clean footage before each finding")
    ap.add_argument("--pad-after", type=float, default=3.0, help="seconds of clean footage after each finding")
    ap.add_argument("--max-clip", type=float, default=12.0, help="cap on total clip length in seconds")
    args = ap.parse_args()

    build_corpus(args.media, args.out, args.pad_before, args.pad_after, args.max_clip)


if __name__ == "__main__":
    main()
