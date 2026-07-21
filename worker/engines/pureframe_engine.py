"""PureFrame adapter.

Drives the `pureframe` CLI (same virtualenv) and converts its censor plan
into the standard timeline format. Detects nudity, sexual activity and
intense kissing; its remediation is a localized tracked blur, so the
recommended action for every finding is "blur".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any, Optional

from ..models import Segment, Timeline
from .base import EngineAdapter, ProgressCb

# PureFrame verdict categories -> standard timeline categories
CATEGORY_MAP = {
    "NUDITY": "nudity",
    "PARTIAL_NUDITY": "nudity",
    "SEXUAL_ACTIVITY": "sexual_activity",
    "AUDIO_MOAN": "sexual_activity",
    "KISSING": "intense_kissing",
    "COMPOSITE": "sexual_activity",
}

_PERCENT_RE = re.compile(r"(\d{1,3})%")


class PureFrameEngine(EngineAdapter):
    name = "pureframe"

    def _cli(self) -> list[str]:
        exe = shutil.which("pureframe", path=str(Path(sys.executable).parent))
        if exe:
            return [exe]
        return [sys.executable, "-m", "pureframe"]

    def version(self) -> str:
        try:
            return pkg_version("pureframe")
        except PackageNotFoundError:
            return "not installed"

    def health(self) -> dict[str, Any]:
        try:
            v = pkg_version("pureframe")
            return {"available": True, "version": v}
        except PackageNotFoundError:
            return {"available": False, "error": "pureframe package not installed"}

    def capabilities(self) -> dict[str, Any]:
        return {
            "categories": ["nudity", "sexual_activity", "intense_kissing"],
            "actions": ["blur"],
            "options": {
                "contentType": ["live-action", "animation", "anime", "low-light"],
                "strictness": ["low", "medium", "high"],
                "threshold": "float 0.0-1.0",
                "profile": ["cpu", "low", "medium", "high"],
            },
        }

    def analyze(
        self,
        media_path: Path,
        fingerprint: str,
        options: dict[str, Any],
        progress: ProgressCb,
    ) -> tuple[Timeline, Optional[Path]]:
        plan_path = media_path.with_name(media_path.stem + ".censorplan.json")
        cmd = self._cli() + ["plan", str(media_path), "--output", str(plan_path), "--verbose"]
        if options.get("contentType"):
            cmd += ["--content-type", str(options["contentType"])]
        if options.get("strictness"):
            cmd += ["--strictness", str(options["strictness"])]
        if options.get("threshold") is not None:
            cmd += ["--threshold", str(options["threshold"])]
        if options.get("profile"):
            cmd += ["--profile", str(options["profile"]).upper()]

        progress(0.0, "starting pureframe plan")
        self._run(cmd, progress)

        if not plan_path.exists():
            raise RuntimeError(
                f"pureframe plan finished but no censor plan found at {plan_path}"
            )

        progress(0.95, "converting censor plan to timeline")
        timeline = self.plan_to_timeline(plan_path, fingerprint)
        progress(1.0, "analysis complete")
        return timeline, plan_path

    def plan_to_timeline(self, plan_path: Path, fingerprint: str) -> Timeline:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        shots = {s["index"]: s for s in plan.get("shots", [])}
        segments: list[Segment] = []
        seg_id = 0
        for verdict in plan.get("verdicts", []):
            action = (verdict.get("action") or "NONE").upper()
            if action == "NONE":
                continue
            shot = shots.get(verdict.get("shot_index"))
            if not shot:
                continue
            seg_id += 1
            segments.append(
                Segment(
                    id=seg_id,
                    startMs=int(float(shot["start_time"]) * 1000),
                    endMs=int(float(shot["end_time"]) * 1000),
                    category=CATEGORY_MAP.get(
                        (verdict.get("category") or "").upper(),
                        (verdict.get("category") or "unknown").lower(),
                    ),
                    confidence=float(verdict.get("confidence") or 0.0),
                    engine=self.name,
                    recommendedAction="blur",
                    approved=None,
                    reasoning=verdict.get("reasoning"),
                    engineRef=str(verdict.get("shot_index")),
                )
            )
        return Timeline(mediaFingerprint=fingerprint, segments=segments)

    def render(
        self,
        media_path: Path,
        plan_path: Path,
        timeline: Timeline,
        output_path: Path,
        progress: ProgressCb,
    ) -> Path:
        # Apply review decisions: any segment explicitly rejected by the
        # administrator is whitelisted (action -> NONE) in a copy of the plan.
        # The original plan and the original media are never modified.
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        rejected_shots = {
            int(seg.engineRef)
            for seg in timeline.segments
            if seg.approved is False and seg.engineRef is not None
        }
        for verdict in plan.get("verdicts", []):
            if verdict.get("shot_index") in rejected_shots:
                verdict["action"] = "NONE"

        reviewed_plan = plan_path.with_name(plan_path.stem + ".reviewed.json")
        reviewed_plan.write_text(json.dumps(plan, indent=2), encoding="utf-8")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = self._cli() + [
            "apply",
            str(media_path),
            str(reviewed_plan),
            "--output",
            str(output_path),
        ]
        progress(0.0, "rendering clean copy")
        self._run(cmd, progress)
        if not output_path.exists():
            raise RuntimeError(f"pureframe apply finished but {output_path} was not created")
        progress(1.0, "render complete")
        return output_path

    def _run(self, cmd: list[str], progress: ProgressCb) -> None:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            tail[:] = tail[-40:]
            match = _PERCENT_RE.search(line)
            if match:
                pct = min(100, int(match.group(1)))
                # analysis progress occupies 0..0.95 of the job
                progress(pct / 100 * 0.95, line[:200])
            else:
                progress(None, line[:200])
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(cmd[:2])} failed (exit {proc.returncode}):\n" + "\n".join(tail)
            )
