"""Score a candidate detector against eval/build_corpus.py's labeled corpus.

The corpus (`eval/corpus/v1/manifest.json` + its `clips/*.mp4`) records, for
every clip, exactly which millisecond range is the approved finding (the
positive region) and which is clean padding a candidate should NOT flag. This
script runs one or more candidate detectors against every clip whose category
they claim to handle and reports, per category:

- **recall** — fraction of positive regions the candidate actually caught.
- **false-positive rate** — fraction of clips where the candidate flagged the
  padding instead of (or in addition to) the positive region.
- **latency per model call** — the real cost of the approach, not just an
  accuracy number, so a slower-but-better or faster-but-worse trade-off is
  visible instead of picking a winner on one axis alone.

See "Detection Accuracy Testing Plan" in
plan/prds/2026-08-29-vidangel-filter-taxonomy.md for why this exists and what
it's for: this is meant to be re-run after every guidance/taxonomy/backend
change, not a one-time check.

Usage:
    uv run python eval/run_comparison.py
    uv run python eval/run_comparison.py --model qwen3-vl:4b-instruct --model moondream2
    uv run python eval/run_comparison.py --corpus eval/corpus/v1 --host http://100.95.155.5:11434 --out eval/results/2026-08-30.json

The candidate interface (`Candidate.analyze_clip`) is deliberately
backend-agnostic: today's Ollama single-call VLM is the only one implemented,
but a perceive-then-classify rewrite or an MLX server can plug in the same
way — return the hits found in one clip plus how long the model actually
took, and this harness does the scoring. (FastVLM was tried and ruled out:
its weights are licensed for Research Purposes only, and its smallest
checkpoint scored 0% recall on this project's exact structured-JSON
observation schema despite correctly describing flagged content in free
text — an instruction-following gap, not a vision one.)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker.engines.vlm_engine import DEFAULT_HOST, DEFAULT_MODEL, VLMEngine  # noqa: E402


@dataclass
class Hit:
    start_ms: int
    end_ms: int
    category: str
    confidence: float


@dataclass
class ClipResult:
    hits: list[Hit]
    calls: int  # model calls made analyzing this clip (~frames sampled)
    call_s: float  # time actually spent waiting on the model, excluding frame grab/decode


class Candidate(ABC):
    """A pluggable detector under test."""

    name: str
    # Corpus categories this candidate can possibly detect. A clip whose
    # source_category isn't in here is skipped for this candidate rather than
    # scored as a miss — e.g. a profanity clip isn't something a vision-only
    # candidate was ever asked about.
    categories: frozenset[str]

    @abstractmethod
    def analyze_clip(self, clip_path: Path) -> ClipResult: ...


class _CountingVLMEngine(VLMEngine):
    """VLMEngine that also times and counts its own model calls.

    analyze()'s wall time includes frame-grab/decode and shot detection, which
    would understate "latency per call" as a proxy for the model itself — this
    isolates the time actually spent waiting on Ollama.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.call_s = 0.0

    def _ask(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return super()._ask(*args, **kwargs)
        finally:
            self.calls += 1
            self.call_s += time.perf_counter() - t0


class OllamaVLMCandidate(Candidate):
    """Today's shipped detector: one VLM call per sampled frame, classified by
    worker.policy.classify() (see worker/engines/vlm_engine.py). Runs the real
    production analyze() path — same shot detection, same prompt, same
    checkpointing — against one short clip instead of a full film.

    Swapping just `model` is enough to try Moondream2 (already in Ollama's
    library) with no other code change, per the PRD's "zero integration cost"
    claim for that candidate.
    """

    categories = frozenset({"nudity", "sexual_activity", "intense_kissing", "suggestive"})

    def __init__(self, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST, **extra_options):
        self.name = f"ollama:{model}"
        self._options = {"model": model, "host": host, **extra_options}

    def analyze_clip(self, clip_path: Path) -> ClipResult:
        engine = _CountingVLMEngine()
        timeline, _cache_path = engine.analyze(
            clip_path, fingerprint="eval", options=self._options, progress=lambda *_: None
        )
        hits = [Hit(s.startMs, s.endMs, s.category, s.confidence) for s in timeline.segments]
        return ClipResult(hits=hits, calls=engine.calls, call_s=engine.call_s)


@dataclass
class CategoryStats:
    category: str
    total_clips: int = 0
    true_positives: int = 0
    false_negatives: int = 0
    false_positive_clips: int = 0
    skipped: int = 0
    errors: int = 0

    @property
    def recall(self) -> Optional[float]:
        return self.true_positives / self.total_clips if self.total_clips else None

    @property
    def false_positive_rate(self) -> Optional[float]:
        return self.false_positive_clips / self.total_clips if self.total_clips else None


def _clip_relative_positive(record: dict) -> tuple[int, int, int]:
    """The manifest's positive_start/end_ms are offsets into combined.mp4;
    each candidate analyzes the individual clip file instead, which always
    starts at 0. Also returns the clip's own duration, needed to define the
    padding-after region."""
    offset = record["combined_start_ms"]
    duration = record["combined_end_ms"] - offset
    return record["positive_start_ms"] - offset, record["positive_end_ms"] - offset, duration


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _leaks_into_padding(hits: list[Hit], pos_start_ms: int, pos_end_ms: int, clip_duration_ms: int) -> bool:
    """True if any hit touches the clean padding before/after the positive
    region -- regardless of whether that same hit ALSO correctly covers the
    positive region.

    A hit spanning the whole clip technically "overlaps the positive region",
    so checking only for hits entirely outside it (as an earlier version of
    this function did) let a candidate that flags everything, all the time,
    score a 0% false-positive rate -- it never said anything CORRECTLY, it
    just never said nothing either. What actually matters for a content
    filter is whether it stays quiet during the clean part, not just whether
    it eventually mentions the right window somewhere in a broad flag.
    """
    return any(
        _overlaps(h.start_ms, h.end_ms, 0, pos_start_ms)
        or _overlaps(h.start_ms, h.end_ms, pos_end_ms, clip_duration_ms)
        for h in hits
    )


def score_candidate(
    candidate: Candidate, manifest: dict, corpus_dir: Path, limit: Optional[int] = None
) -> dict:
    per_category: dict[str, CategoryStats] = {}
    clip_reports: list[dict] = []
    total_calls = 0
    total_call_s = 0.0
    analyzed = 0

    for record in manifest["clips"]:
        category = record["source_category"]
        stats = per_category.setdefault(category, CategoryStats(category=category))
        if category not in candidate.categories:
            stats.skipped += 1
            continue
        if limit is not None and analyzed >= limit:
            stats.skipped += 1
            continue

        clip_path = corpus_dir / record["clip_file"]
        pos_start_ms, pos_end_ms, clip_duration_ms = _clip_relative_positive(record)

        # A single flaky call (a stalled host, a dropped share read) must not
        # discard every clip already scored in this run — record it as an
        # error, excluded from recall/false-positive totals, and move on.
        try:
            result = candidate.analyze_clip(clip_path)
        except Exception as exc:  # noqa: BLE001
            analyzed += 1
            stats.errors += 1
            print(f"  ERROR {record['clip_file']} ({category}): {exc}", file=sys.stderr)
            clip_reports.append({"clip_file": record["clip_file"], "category": category, "error": str(exc)})
            continue

        analyzed += 1
        total_calls += result.calls
        total_call_s += result.call_s

        hit_in_positive = any(_overlaps(h.start_ms, h.end_ms, pos_start_ms, pos_end_ms) for h in result.hits)
        hit_in_padding = _leaks_into_padding(result.hits, pos_start_ms, pos_end_ms, clip_duration_ms)

        stats.total_clips += 1
        if hit_in_positive:
            stats.true_positives += 1
        else:
            stats.false_negatives += 1
        if hit_in_padding:
            stats.false_positive_clips += 1

        avg_call_s = result.call_s / result.calls if result.calls else 0.0
        print(
            f"  [{analyzed}] {record['clip_file']} ({category}): "
            f"{'caught' if hit_in_positive else 'MISSED'}"
            f"{', false positive on padding' if hit_in_padding else ''}"
            f" — {result.calls} call(s), {avg_call_s:.2f}s/call",
            file=sys.stderr,
        )

        clip_reports.append(
            {
                "clip_file": record["clip_file"],
                "category": category,
                "caught": hit_in_positive,
                "false_positive": hit_in_padding,
                "hits": [vars(h) for h in result.hits],
            }
        )

    skipped_categories = Counter(
        {cat: s.skipped for cat, s in per_category.items() if s.skipped and s.total_clips == 0}
    )

    return {
        "candidate": candidate.name,
        "categories": {
            cat: {
                "total_clips": s.total_clips,
                "true_positives": s.true_positives,
                "false_negatives": s.false_negatives,
                "false_positive_clips": s.false_positive_clips,
                "recall": s.recall,
                "false_positive_rate": s.false_positive_rate,
                "skipped": s.skipped,
                "errors": s.errors,
            }
            for cat, s in sorted(per_category.items())
        },
        "not_applicable_categories": dict(skipped_categories),
        "total_calls": total_calls,
        "avg_latency_per_call_s": total_call_s / total_calls if total_calls else None,
        "clips": clip_reports,
    }


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x * 100:.1f}%" if x is not None else "-"


def print_report(report: dict) -> None:
    print(f"\n=== {report['candidate']} ===")
    print(f"{'category':<20} {'clips':>6} {'recall':>8} {'fp-rate':>8} {'errors':>7}")
    for cat, s in report["categories"].items():
        if s["total_clips"] == 0 and s["errors"] == 0:
            continue
        print(
            f"{cat:<20} {s['total_clips']:>6} {_fmt_pct(s['recall']):>8} "
            f"{_fmt_pct(s['false_positive_rate']):>8} {s['errors']:>7}"
        )
    if report["not_applicable_categories"]:
        skipped = ", ".join(f"{cat} x{n}" for cat, n in report["not_applicable_categories"].items())
        print(f"(not applicable to this candidate — skipped: {skipped})")
    latency = report["avg_latency_per_call_s"]
    latency_str = f"{latency:.3f}s" if latency is not None else "-"
    print(f"avg latency per model call: {latency_str} over {report['total_calls']} call(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=Path("eval/corpus/v1"), help="corpus directory (from build_corpus.py)")
    ap.add_argument("--model", action="append", dest="models", help="Ollama model tag to test (repeatable)")
    ap.add_argument("--host", default=DEFAULT_HOST, help="Ollama base URL")
    ap.add_argument("--limit", type=int, default=None, help="only analyze the first N applicable clips per candidate")
    ap.add_argument("--out", type=Path, default=None, help="write the full JSON report here")
    args = ap.parse_args()

    manifest_path = args.corpus / "manifest.json"
    if not manifest_path.exists():
        print(f"no manifest at {manifest_path} — run eval/build_corpus.py first", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    models = args.models or [DEFAULT_MODEL]
    candidates: list[Candidate] = [OllamaVLMCandidate(model=m, host=args.host) for m in models]

    reports = []
    for candidate in candidates:
        report = score_candidate(candidate, manifest, args.corpus, limit=args.limit)
        print_report(report)
        reports.append(report)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(f"\nfull report written to {args.out}")


if __name__ == "__main__":
    main()
