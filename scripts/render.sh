#!/usr/bin/env bash
# Render a clean copy from a film's APPROVED findings.
#
# Rejected and undecided findings are ignored, so review first. The
# original is never modified; output goes to movies/cleaned/.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
cd "$(dirname "$0")/.."

if [ $# -lt 1 ]; then
  echo "usage: $0 <film.mkv> [output.mkv]" >&2
  exit 1
fi

"$UV" run python -c '
import sys
from pathlib import Path
from worker.models import Timeline
from worker.render import approved_for_render, render
from worker.review import load_timeline
from worker.shots import true_fps

media = Path(sys.argv[1])
timeline = load_timeline(media)
if timeline is None:
    sys.exit(f"no analysis found for {media.name} - run scripts/analyze.sh first")

approved = approved_for_render(timeline)
if not approved:
    sys.exit(
        f"{media.name}: none of {len(timeline.segments)} finding(s) are approved.\n"
        "Review them first: scripts/review.sh " + str(media)
    )

out = Path(sys.argv[2]) if len(sys.argv) > 2 else media.parent / "cleaned" / f"{media.stem} (Clean){media.suffix}"
_, duration, _ = true_fps(media)
print(f"{len(approved)} approved finding(s) -> {out}")
render(media, Timeline(mediaFingerprint=timeline.mediaFingerprint, segments=approved),
       out, lambda f, s: print(f"  {s}", flush=True), duration_s=duration)
' "$@"
