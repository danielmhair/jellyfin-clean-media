"""Vision-language model adapter (Qwen2.5-VL via Ollama).

Replaces fixed-purpose detectors like NudeNet with a general VLM that is
told, in plain language, what to look for. The categories are therefore
configurable per job rather than baked into model weights.

Sampling is shot-based, not interval-based. Film is already segmented into
shots, and within a shot the frame content is largely redundant — sampling
every 0.4s spends roughly five inferences to answer one question. Shot
boundaries are also exactly where a cut belongs, so detections need no
separate boundary-refinement pass. Long takes still get multiple samples;
see shots.sample_times.

The Ollama endpoint is configurable so inference can run on a different
machine from the encoder.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from ..models import Segment, Timeline
from ..policy import OBSERVATIONS, Policy, classify
from ..shots import Shot, detect_shots, load_shots, sample_times, save_shots
from .base import EngineAdapter, ProgressCb

# Most serious first, so a shot is described by its worst moment.
_SEVERITY = {"sexual_activity": 3, "nudity": 2, "intense_kissing": 1, "suggestive": 0}


def _severity(category: str) -> int:
    return _SEVERITY.get(category, 0)

DEFAULT_HOST = "http://localhost:11434"
# Must be an -instruct tag. The bare qwen3-vl tags are thinking models: they
# spend hundreds of tokens reasoning before emitting any content, which is
# pure latency for a yes/no judgement and returns empty content if the
# generation cap is hit mid-thought.
DEFAULT_MODEL = "qwen3-vl:4b-instruct"
# Frames are downscaled before inference: image size drives visual token
# count, which dominates latency far more than model size does.
FRAME_WIDTH = 512

def _prompt_digest(prompt: str) -> str:
    """Stable across processes, unlike hash(), which is seed-randomised."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


DEFAULT_CATEGORIES = {
    "nudity": "a topless or naked woman — including a woman seen from behind "
    "or from the side whose bare back and shoulders show she is undressed, "
    "even though nothing explicit is visible — or anyone's bare buttocks or "
    "genitals. A shirtless man wearing trousers, shorts or swimwear is NOT "
    "nudity, and neither is a man's bare back or shoulders",
    "sexual_activity": "people actively having sex or simulating it. Two people "
    "merely lying near each other, talking, or sleeping is NOT sexual activity",
    "suggestive": "a body presented sexually without explicit nudity — a "
    "shirtless man or a scantily dressed person filmed as an object of "
    "desire, the camera lingering on the body, or someone stripping or posing "
    "seductively. Ordinary shirtlessness with no sexual framing — swimming, "
    "sport, medical, washing, fighting — is NOT this",
    "intense_kissing": "sustained sexual kissing of the kind meant to be "
    "private — open-mouthed, with roaming hands or partial undress. An "
    "ordinary romantic kiss, a peck, or a brief affectionate kiss is NOT this",
}

# Ask the model only what it can see, and decide what counts in code.
#
# Telling a model "a shirtless man is NOT nudity" does not work: on a
# film with a permanently shirtless character it flagged 50 such shots as
# nudity at 0.95-0.98, one of them literally described as "no explicit
# nudity".
# Negative instructions fight the category label, and the label wins.
#
# Observations are also cheap to re-interpret: policy is a pure function
# over them, so changing what counts re-derives instantly instead of
# costing another multi-hour pass over the film.
OBSERVE_PROMPT = """You are looking at one frame from a film. Answer only what you can actually SEE in this frame.

The frame may be dark — look carefully at low-light detail. Judge only real human bodies: statues, paintings, cartoons, toys and costumed or non-human creatures do not count as people. A figure too small, distant or blurred to make out does not count either.

Answer every field with true or false:

- "female_topless": a woman's bare chest or breasts are visible, OR a woman is clearly undressed though turned away
- "buttocks_or_genitals": anyone's bare buttocks or genitals are visible
- "underwear_only": someone is in underwear or lingerie
- "male_shirtless": a man is bare-chested (he may be wearing trousers, shorts or swimwear)
- "sex_act": people are having sex or simulating it
- "kissing": two people are kissing
- "kissing_sexual": that kissing is the sustained, open-mouthed kind meant to be private
- "sexualised_framing": the camera is presenting a body as an object of desire — lingering on it, posing, stripping — rather than incidentally (swimming, sport, fighting, washing, medical)

Respond with JSON only:
{"female_topless": false, "buttocks_or_genitals": false, "underwear_only": false, "male_shirtless": false, "sex_act": false, "kissing": false, "kissing_sexual": false, "sexualised_framing": false, "description": "<what you see, under 15 words>"}"""

PROMPT = """You are reviewing a single frame from a film to help a parent decide what to skip.

The frame may be dark or dimly lit — look carefully at low-light detail before deciding. Judge what is shown, including what is clearly happening even when partly obscured by bedding, shadow or framing.

Report only real human bodies. Statues, sculptures, paintings, cartoons, toys and costumed or non-human creatures are NOT nudity, however anatomical they look.

When a body is shown but nothing explicit is visible, prefer "suggestive" over "nudity" — an administrator reviews those separately, so it is better to raise a weaker flag than to force the choice between a false alarm and silence.

Judge only what is clearly visible. A figure that is small, distant, blurred or in the background is not enough to report — if you cannot actually make out the body, answer false.

Do NOT report violence, weapons, blood, or people who are simply clothed.

Categories to detect:
{categories}

Respond with JSON only:
{{"detected": false}}
or
{{"detected": true, "category": "<one of: {names}>", "confidence": <0.0-1.0>, "description": "<what you see, under 15 words>"}}"""


class VLMEngine(EngineAdapter):
    name = "vlm"

    def version(self) -> str:
        return "1.0"

    def _host(self, options: dict[str, Any]) -> str:
        return str(options.get("host", DEFAULT_HOST)).rstrip("/")

    def health(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{DEFAULT_HOST}/api/tags", timeout=3) as r:
                tags = json.loads(r.read())
            models = [m["name"] for m in tags.get("models", [])]
            return {"available": True, "host": DEFAULT_HOST, "models": models}
        except Exception as exc:
            return {"available": False, "error": f"no Ollama at {DEFAULT_HOST}: {exc}"}

    def capabilities(self) -> dict[str, Any]:
        return {
            "categories": ["nudity", "sexual_activity", "intense_kissing", "suggestive"],
            # The model reports these observations; the category is decided
            # in worker/policy.py, so what counts can change without
            # re-analyzing a film.
            "observations": list(OBSERVATIONS),
            # "suggestive" is a deliberately weaker signal: a body shown
            # without explicit nudity, which is objectionable or not
            # depending on how the film frames it. Surfaced for review
            # rather than resolved by the model.
            "tentativeCategories": ["suggestive"],
            "actions": ["skip", "blur"],
            "options": {
                "host": "Ollama base URL (default http://localhost:11434)",
                "model": "vision model tag — must be an -instruct variant "
                "(default qwen3-vl:4b-instruct; 2b-instruct is faster)",
                "flagMaleShirtless": "bool — flag every bare male chest, not "
                "only sexualised framing (default false)",
                "flagUnderwear": "bool — flag underwear/lingerie (default true)",
                "flagAnyKissing": "bool — flag all kissing, not only the "
                "private kind (default false)",
                "maxGapS": "max seconds between samples inside a long shot (default 2.5)",
                "minConfidence": "drop detections below this (default 0.5)",
                "action": "skip or blur (default skip)",
                "shots": "path to a cached shot list",
            },
        }

    # -- frame extraction ---------------------------------------------------

    def _grab(self, media_path: Path, when_s: float) -> Optional[bytes]:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-ss", f"{when_s:.3f}", "-i", str(media_path),
                "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-",
            ],
            capture_output=True,
        )
        return self._boost_if_dark(proc.stdout) if proc.stdout else None

    @staticmethod
    def _boost_if_dark(jpeg: bytes, threshold: float = 45.0) -> bytes:
        """Lift near-black frames before inference.

        Night interiors land around mean luma 15 and the model reports
        nothing at all on them — every miss in the first benchmark was a
        frame this dark. CLAHE recovers local detail without blowing out
        the highlights the way a flat gamma curve does.
        """
        try:
            import cv2
            import numpy as np
        except ImportError:
            return jpeg

        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None or cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean() >= threshold:
            return jpeg

        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        lightness, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        merged = cv2.merge((clahe.apply(lightness), a, b))
        boosted = cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
        ok, buf = cv2.imencode(".jpg", boosted, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes() if ok else jpeg

    def _ask(self, host: str, model: str, jpeg: bytes, prompt: str) -> dict:
        payload = json.dumps(
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [base64.b64encode(jpeg).decode()],
                    }
                ],
                "stream": False,
                "format": "json",
                "keep_alive": "30m",
                "options": {"temperature": 0.0, "num_predict": 300},
            }
        ).encode()
        req = urllib.request.Request(
            f"{host}/api/chat", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read())
        message = body.get("message") or {}
        content = (message.get("content") or "").strip()
        if not content and message.get("thinking"):
            raise RuntimeError(
                f"{model} returned only reasoning and no answer — it is a "
                "'thinking' model. Use an -instruct tag instead."
            )
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"detected": False, "_unparsed": content[:200]}

    # -- analysis -----------------------------------------------------------

    def analyze(
        self,
        media_path: Path,
        fingerprint: str,
        options: dict[str, Any],
        progress: ProgressCb,
    ) -> tuple[Timeline, Optional[Path]]:
        host = self._host(options)
        model = options.get("model", DEFAULT_MODEL)
        max_gap = float(options.get("maxGapS", 2.5))
        action = options.get("action", "skip")

        cache = Path(options["shots"]) if options.get("shots") else media_path.with_name(
            media_path.stem + ".shots.json"
        )
        if cache.exists():
            shots = load_shots(cache)
            progress(0.02, f"loaded {len(shots)} cached shots")
        else:
            progress(0.0, "detecting shots")
            shots = detect_shots(media_path)
            save_shots(shots, cache)
            progress(0.05, f"detected {len(shots)} shots")

        policy = Policy(
            flag_male_shirtless=bool(options.get("flagMaleShirtless", False)),
            flag_underwear=bool(options.get("flagUnderwear", True)),
            flag_any_kissing=bool(options.get("flagAnyKissing", False)),
        )
        prompt = OBSERVE_PROMPT

        min_samples = int(options.get("minSamples", 2))
        plan = [
            (shot, t)
            for shot in shots
            for t in sample_times(shot, max_gap, min_samples)
        ]
        progress(0.05, f"{len(plan)} samples across {len(shots)} shots")

        # A full-film pass is hours long; checkpoint so an interruption
        # costs minutes rather than the whole run.
        checkpoint = media_path.with_name(media_path.stem + ".vlm-progress.json")
        hits: dict[int, dict] = {}
        done: set[str] = set()
        if checkpoint.exists() and not options.get("restart"):
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            # Verdicts are only comparable within one model and prompt.
            # Resuming across a model change would silently blend a weak
            # model's judgements into a strong model's run.
            if saved.get("model") == model and saved.get("prompt") == _prompt_digest(prompt):
                hits = {int(k): v for k, v in saved.get("hits", {}).items()}
                done = set(saved.get("done", []))
                progress(0.05, f"resuming: {len(done)} samples already done")
            else:
                progress(0.05, "model or prompt changed — discarding old progress")

        def save_checkpoint() -> None:
            checkpoint.write_text(
                json.dumps(
                    {
                        "model": model,
                        "prompt": _prompt_digest(prompt),
                        "hits": hits,
                        "done": sorted(done),
                    }
                ),
                encoding="utf-8",
            )

        by_shot: dict[int, Shot] = {s.index: s for s in shots}
        for n, (shot, when) in enumerate(plan, 1):
            key = f"{shot.index}:{when:.2f}"
            if key in done:
                continue
            jpeg = self._grab(media_path, when)
            if not jpeg:
                continue
            try:
                verdict = self._ask(host, model, jpeg, prompt)
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(f"Ollama request failed: {exc}") from exc

            done.add(key)
            # Store the observations, not a verdict. Policy is applied below
            # and can be changed later without re-analyzing the film.
            observations = {k: bool(verdict.get(k)) for k in OBSERVATIONS}
            category = classify(observations, policy)
            if category is not None:
                prev = hits.get(shot.index)
                # Prefer the more serious finding within a shot; ties keep
                # the first, so a shot is described by its worst moment.
                if not prev or _severity(category) > _severity(prev["category"]):
                    hits[shot.index] = {
                        "category": category,
                        "confidence": 1.0 if category != "suggestive" else 0.5,
                        "description": verdict.get("description", ""),
                        "observations": observations,
                        "at": when,
                    }
                    save_checkpoint()  # never lose a detection
            if n % 25 == 0 or n == len(plan):
                save_checkpoint()
                progress(
                    0.05 + 0.93 * n / len(plan),
                    f"{n}/{len(plan)} samples, {len(hits)} shots flagged",
                )

        groups = self._group_into_scenes(
            sorted(hits.items()), by_shot, int(options.get("bridgeShots", 2))
        )
        segments = []
        for i, group in enumerate(groups, 1):
            indices = [idx for idx, _ in group]
            best = max((h for _, h in group), key=lambda h: h["confidence"])
            # Detection rarely covers a scene's first and last shot, so pad
            # outward: over-cutting by one shot is far cheaper than leaving
            # the tail of a scene in.
            pad = int(options.get("padShots", 1))
            lo = max(min(by_shot), min(indices) - pad)
            hi = min(max(by_shot), max(indices) + pad)
            start = by_shot[lo].start_s
            end = by_shot[hi].end_s
            label = (
                f"shot {indices[0]}"
                if len(indices) == 1
                else f"shots {min(indices)}-{max(indices)}"
            )
            segments.append(
                Segment(
                    id=i,
                    startMs=int(start * 1000),
                    endMs=int(end * 1000),
                    category=best["category"],
                    confidence=round(best["confidence"], 3),
                    engine=self.name,
                    recommendedAction=action,
                    approved=None,
                    reasoning=f"{label} ({len(indices)} flagged): {best['description']}",
                    engineRef=",".join(str(i) for i in indices),
                )
            )
        progress(1.0, f"{len(hits)} shot(s) flagged in {len(segments)} scene(s)")
        return Timeline(mediaFingerprint=fingerprint, segments=segments), cache

    @staticmethod
    def _group_into_scenes(flagged, by_shot: dict[int, Shot], bridge: int = 2):
        """Group flagged shots into scenes, bridging small unflagged gaps.

        Per-shot recall is not the goal — an objectionable scene spans
        several shots and the model only has to catch some of them. Bridging
        gaps of up to `bridge` shots turns partial detection into full scene
        coverage, and the cut still lands on real shot boundaries.
        """
        groups: list[list] = []
        for item in flagged:
            index = item[0]
            if groups and index - groups[-1][-1][0] <= bridge + 1:
                groups[-1].append(item)
            else:
                groups.append([item])
        return groups

    def render(
        self,
        media_path: Path,
        plan_path: Path,
        timeline: Timeline,
        output_path: Path,
        progress: ProgressCb,
    ) -> Path:
        from ..render import render as render_timeline

        from ..shots import true_fps

        _, duration, _ = true_fps(media_path)
        return render_timeline(
            media_path, timeline, output_path, progress, duration_s=duration
        )
