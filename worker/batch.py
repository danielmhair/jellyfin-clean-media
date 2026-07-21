"""Batch-analyze a library.

Runs the cheap engines before the expensive one, writes a merged
`<name>.cleanmedia.json` per film, and skips work that is already done — so
re-running after adding films, or after an interruption, costs only the new
work.

    uv run python -m worker.batch "movies/*.mkv"
    uv run python -m worker.batch --engines subtitles "movies/*.mkv"
    uv run python -m worker.batch --host http://100.95.155.5:11434 movies/
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

from .engines import ENGINES
from .models import Segment, Timeline
from .store import media_fingerprint

VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov"}
# Cheapest and most accurate first: if the audio work is done, a crash
# during the long visual pass still leaves something useful behind.
DEFAULT_ENGINES = ("subtitles", "vlm")


def discover(patterns: list[str]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        path = Path(pattern)
        if path.is_dir():
            found += [p for p in sorted(path.rglob("*")) if p.suffix.lower() in VIDEO_SUFFIXES]
        elif path.is_file():
            found.append(path)
        else:
            found += [Path(p) for p in sorted(glob.glob(pattern)) if Path(p).is_file()]
    # A rendered copy is an output, never an input.
    return [p for p in dict.fromkeys(found) if "(Clean)" not in p.name]


def analyze_one(
    media: Path, engines: list[str], options: dict, force: bool
) -> Timeline | None:
    sidecar = media.with_name(media.stem + ".cleanmedia.json")
    existing: dict[str, list[Segment]] = {}
    if sidecar.exists() and not force:
        prior = Timeline.model_validate_json(sidecar.read_text(encoding="utf-8"))
        for segment in prior.segments:
            existing.setdefault(segment.engine, []).append(segment)

    fingerprint = media_fingerprint(media)
    merged: list[Segment] = []
    for name in engines:
        if name in existing:
            print(f"    {name}: {len(existing[name])} segment(s) (cached)", flush=True)
            merged += existing[name]
            continue

        engine = ENGINES[name]
        started = time.time()
        last = [""]

        def progress(frac, stage, _last=last):
            if stage and stage != _last[0]:
                print(f"    {name}: {stage}", flush=True)
                _last[0] = stage

        try:
            timeline, _ = engine.analyze(media, fingerprint, options.get(name, {}), progress)
        except Exception as exc:  # noqa: BLE001 — one bad film must not stop the batch
            print(f"    {name}: FAILED — {exc}", flush=True)
            continue

        elapsed = time.time() - started
        print(
            f"    {name}: {len(timeline.segments)} segment(s) in {elapsed / 60:.1f} min",
            flush=True,
        )
        merged += timeline.segments

    if not merged:
        return None
    merged.sort(key=lambda s: s.startMs)
    for i, segment in enumerate(merged, 1):
        segment.id = i
    timeline = Timeline(mediaFingerprint=fingerprint, segments=merged)
    sidecar.write_text(json.dumps(timeline.model_dump(), indent=2), encoding="utf-8")
    return timeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="files, folders or globs")
    parser.add_argument("--engines", default=",".join(DEFAULT_ENGINES))
    parser.add_argument("--host", help="Ollama base URL for the vlm engine")
    parser.add_argument("--model", default="qwen3-vl:4b-instruct")
    parser.add_argument("--max-gap", type=float, default=6.0)
    parser.add_argument("--min-samples", type=int, default=1)
    parser.add_argument("--force", action="store_true", help="ignore cached results")
    args = parser.parse_args(argv)

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    unknown = [e for e in engines if e not in ENGINES]
    if unknown:
        parser.error(f"unknown engine(s): {unknown}; available: {sorted(ENGINES)}")

    options = {
        "subtitles": {"includeMild": True, "includeBlasphemy": True},
        "whisper": {"includeMild": True, "includeBlasphemy": True},
        "vlm": {
            "model": args.model,
            "maxGapS": args.max_gap,
            "minSamples": args.min_samples,
            **({"host": args.host} if args.host else {}),
        },
    }

    films = discover(args.paths)
    if not films:
        print("no video files matched", file=sys.stderr)
        return 1

    print(f"{len(films)} file(s); engines: {', '.join(engines)}\n", flush=True)
    started = time.time()
    for n, media in enumerate(films, 1):
        print(f"[{n}/{len(films)}] {media.name}", flush=True)
        timeline = analyze_one(media, engines, options, args.force)
        if timeline:
            actions = {}
            for segment in timeline.segments:
                actions[segment.recommendedAction] = actions.get(segment.recommendedAction, 0) + 1
            print(f"    -> {len(timeline.segments)} finding(s) {actions}\n", flush=True)
        else:
            print("    -> nothing found\n", flush=True)

    print(f"done in {(time.time() - started) / 3600:.1f} h", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
