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
import os
import queue as queuelib
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ..models import Segment, Timeline
from ..policy import DEFAULT_FIELD_GUIDANCE, OBSERVATIONS, Policy, classify, observe_json_footer
from ..settings import get_settings
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
# Layers to offload to the GPU. 37 = all 36 transformer blocks + the output
# layer, i.e. the whole model on the card. See analyze() for why we override
# Ollama's (too-conservative) auto-split on a small-VRAM GPU.
DEFAULT_NUM_GPU = 37
# Frames are downscaled before inference: image size drives visual token
# count, which dominates latency far more than model size does.
FRAME_WIDTH = 512

# Whether this machine's own GPU is currently driving inference for a running
# visual pass. Whisper (and the subtitle engine's precise-timing pass) load
# their own CUDA model on this SAME machine, so if one starts while this
# host is still working a visual pass, they fight the local Ollama host for
# the one card instead of genuinely running in parallel -- the queue's two
# lanes are only independent resources when the visual pass's local host and
# whisper's CUDA aren't the same physical GPU. The general lane checks this
# before claiming a whisper job (see queue.py); dangaming2-style remote hosts
# are unaffected and keep helping the visual pass regardless.
_local_lock = threading.Lock()
_local_workers = 0


def _is_local_host(host: str) -> bool:
    try:
        return urlparse(host).hostname in ("localhost", "127.0.0.1", "::1")
    except ValueError:
        return False


def local_host_busy() -> bool:
    """True while a worker thread is actively using this machine's GPU for a running visual pass."""
    with _local_lock:
        return _local_workers > 0


def _local_host_worker_started() -> None:
    global _local_workers
    with _local_lock:
        _local_workers += 1


def _local_host_worker_stopped() -> None:
    global _local_workers
    with _local_lock:
        _local_workers -= 1


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
# The calibration paragraphs — always present, never editable from the
# plugin. This is what stops the model drifting on statues/mannequins/dim
# lighting; letting an admin edit *this* part is what would actually risk
# breaking detection, unlike the per-field guidance below.
_OBSERVE_INTRO = """You are looking at one frame from a film. Report only what is plainly visible in THIS frame. When genuinely uncertain, answer false — a human reviews every true.

Ignore statues, sculptures, mannequins, paintings, cartoons, toys, food, and costumed or non-human creatures, however anatomical or flesh-coloured they look. Judge only real human bodies. A distant or dimly lit real person still counts — look carefully at low-light and small figures before deciding. Clothing in an unusual colour (a jumpsuit, bodysuit, or tight outfit) is still clothing, not bare skin, even where it hugs the body."""


def _observe_prompt() -> str:
    """Assemble the VLM's detection prompt from fixed + admin-editable parts.

    Only the per-field definitions (what counts as "female_topless",
    "sexualised_framing", etc.) come from the settings store — the
    calibration intro above and the JSON-schema footer
    (:func:`worker.policy.observe_json_footer`) are always the code's own
    text, so no plugin edit can ever break the JSON contract that
    worker.policy.classify() depends on; at worst a bad edit makes one
    field's judgement worse, never crashes the pass.
    """
    overrides = get_settings().vlmGuidance
    lines = [
        f'- "{key}": {(overrides.get(key) or "").strip() or DEFAULT_FIELD_GUIDANCE[key]}'
        for key in OBSERVATIONS
    ]
    return (
        _OBSERVE_INTRO
        + "\n\nAnswer each field true only if the stated evidence is actually visible:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + observe_json_footer()
    )

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
    # The multi-hour visual pass writes a .vlm-progress.json checkpoint every 25
    # samples and resumes from it, so it is safe to pause and re-queue.
    resumable = True

    def version(self) -> str:
        return "1.0"

    def _host(self, options: dict[str, Any]) -> str:
        return str(options.get("host", DEFAULT_HOST)).rstrip("/")

    def _hosts(self, options: dict[str, Any]) -> list[str]:
        """The Ollama endpoints to fan inference out across.

        One film's samples are dispatched concurrently over this list, so two
        machines' GPUs work the same pass at once. Precedence: an explicit
        ``hosts`` option (list, or comma-separated string) wins; otherwise, if
        no single ``host`` was given, the settings store's ``vlmHosts`` (set
        from the plugin's Advanced tab) wins, then the ``CLEANMEDIA_VLM_HOSTS``
        env var (the pre-plugin way to set the pool once on the worker
        machine), then a single ``host`` (default localhost). Duplicates and
        blanks are dropped, order kept, so a one-host pool behaves exactly
        like the old serial path.
        """
        raw = options.get("hosts")
        if raw:
            candidates = raw if isinstance(raw, list) else str(raw).split(",")
        elif options.get("host") is None:
            pool = get_settings().vlmHosts.strip() or os.environ.get("CLEANMEDIA_VLM_HOSTS", "")
            candidates = pool.split(",") if pool else [self._host(options)]
        else:
            candidates = [self._host(options)]
        hosts: list[str] = []
        for h in candidates:
            h = str(h).strip().rstrip("/")
            if h and h not in hosts:
                hosts.append(h)
        return hosts or [DEFAULT_HOST]

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
                "hosts": "list (or comma-separated string) of Ollama base URLs "
                "to fan one film's inference across — e.g. two machines' GPUs. "
                "Falls back to CLEANMEDIA_VLM_HOSTS, then 'host'",
                "concurrencyPerHost": "in-flight requests per host (default 1)",
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

    def _ask(
        self,
        host: str,
        model: str,
        jpeg: bytes,
        prompt: str,
        num_ctx: int = 2048,
        num_gpu: Optional[int] = None,
    ) -> dict:
        # num_ctx matters for fit, not just correctness: one 512px frame plus
        # this prompt and a 300-token reply is well under 2048, and the
        # smaller the context the less KV cache Ollama reserves — which on a
        # 4 GB card is the difference between the model sitting on the GPU and
        # spilling half of itself onto the CPU. Ollama defaults to 4096.
        opts: dict[str, Any] = {
            "temperature": 0.0,
            "num_predict": 300,
            "num_ctx": num_ctx,
        }
        # num_gpu = how many transformer layers Ollama offloads to the GPU.
        # Left unset (None), Ollama auto-picks a conservative split that, on a
        # 4 GB card, leaves layers on the CPU and halves throughput. Forcing it
        # higher pushes more of the model onto the card. Too high just fails the
        # model load (Ollama falls back / the request retries) — so it's safe to
        # tune and trivial to revert by clearing the option.
        if num_gpu is not None:
            opts["num_gpu"] = int(num_gpu)
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
                "options": opts,
            }
        ).encode()
        # Retry rather than let one stalled request abort a multi-hour film.
        # On a VRAM-tight card an occasional inference balloons past the
        # timeout; a short pause and retry almost always clears it, and the
        # per-request timeout is kept well under the old 300s so a stall is
        # caught in a minute, not five.
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    f"{host}/api/chat",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = json.loads(r.read())
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s backoff
        else:
            raise TimeoutError(
                f"Ollama did not answer after 4 attempts: {last_exc}"
            )
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
        hosts = self._hosts(options)
        # In-flight requests per host. One is right for a single small GPU (it
        # can only run one inference at a time); a bigger card can sometimes
        # overlap two. The real parallelism here is across hosts.
        per_host = max(1, int(options.get("concurrencyPerHost", 1)))
        model = options.get("model", DEFAULT_MODEL)
        max_gap = float(options.get("maxGapS", 2.5))
        action = options.get("action", "skip")
        num_ctx = int(options.get("numCtx", 2048))
        # Force full-GPU offload. Ollama's auto-split badly under-fills a 4 GB
        # card — it left ~48% of qwen3-vl:4b on the CPU (13 tok/s) when the whole
        # model actually fits at num_gpu=37 (all 36 blocks + output → 100% GPU,
        # 3.2 GB, ~52 tok/s, a ~3.8x speedup measured on an RTX 3050 4 GB).
        # Override per job via options["numGpu"]; pass null or a negative value
        # to hand the decision back to Ollama's estimator.
        num_gpu = options.get("numGpu", DEFAULT_NUM_GPU)
        num_gpu = int(num_gpu) if num_gpu not in (None, "") else None
        if num_gpu is not None and num_gpu < 0:
            num_gpu = None  # escape hatch: let Ollama auto-decide the split

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

        stored = get_settings()
        policy = Policy(
            flag_male_shirtless=bool(options.get("flagMaleShirtless", stored.flagMaleShirtless)),
            flag_underwear=bool(options.get("flagUnderwear", stored.flagUnderwear)),
            flag_any_kissing=bool(options.get("flagAnyKissing", stored.flagAnyKissing)),
        )
        prompt = _observe_prompt()

        min_samples = int(options.get("minSamples", 2))
        plan = [
            (shot, t)
            for shot in shots
            for t in sample_times(shot, max_gap, min_samples)
        ]
        pool = f" over {len(hosts)} vision hosts" if len(hosts) > 1 else ""
        progress(0.05, f"{len(plan)} samples across {len(shots)} shots{pool}")

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

        def fold(shot: Shot, when: float, verdict: dict) -> None:
            """Record one answered sample. Runs only on the calling thread."""
            done.add(f"{shot.index}:{when:.2f}")
            # Store the observations, not a verdict. Policy is applied below
            # and can be changed later without re-analyzing the film.
            observations = {k: bool(verdict.get(k)) for k in OBSERVATIONS}
            category = classify(observations, policy)
            if category is None:
                return
            prev = hits.get(shot.index)
            # Describe a shot by its worst moment; on equal severity, keep the
            # EARLIEST frame. Ordering by timestamp, not arrival, makes the
            # label deterministic no matter which host answered first — two
            # frames of one shot can complete out of order across the pool.
            better = prev is None or (
                _severity(category),
                -when,
            ) > (
                _severity(prev["category"]),
                -prev["at"],
            )
            if better:
                hits[shot.index] = {
                    "category": category,
                    "confidence": 1.0 if category != "suggestive" else 0.5,
                    "description": verdict.get("description", ""),
                    "observations": observations,
                    "at": when,
                }
                save_checkpoint()  # never lose a detection

        # Fan the remaining samples out across the host pool. The samples form a
        # shared work queue; one worker thread per host slot pulls, grabs the
        # frame and asks its host, and posts the answer back. Answers are folded
        # in HERE, on the analyze() thread — so ``hits``/``done``/the checkpoint
        # stay single-writer, and ``progress`` (which raises to cancel or pause)
        # still unwinds the whole pass. A faster host simply pulls more, so the
        # queue self-balances a 4 GB and a 6 GB card without any tuning.
        pending = [
            (shot, when)
            for shot, when in plan
            if f"{shot.index}:{when:.2f}" not in done
        ]
        already = len(plan) - len(pending)
        if pending:
            work_q: queuelib.Queue = queuelib.Queue()
            for shot, when in pending:
                work_q.put((shot, when, 0))
            result_q: queuelib.Queue = queuelib.Queue()
            stop = threading.Event()
            state_lock = threading.Lock()
            # Try a failed sample on another host before giving up on it, so a
            # single dying machine fails over rather than dropping work.
            max_attempts = len(hosts)
            live = len(hosts) * per_host

            def worker(host: str) -> None:
                nonlocal live
                fails = 0
                is_local = _is_local_host(host)
                if is_local:
                    _local_host_worker_started()
                # The whole body is under try/finally: the ``live`` decrement and
                # the "drained" sentinel MUST fire even if something in here
                # throws, or the consumer blocks forever on result_q waiting for
                # a worker that has silently died — a full-pass freeze.
                try:
                    while not stop.is_set():
                        try:
                            shot, when, attempt = work_q.get_nowait()
                        except queuelib.Empty:
                            break
                        try:
                            jpeg = self._grab(media_path, when)
                            if not jpeg:
                                result_q.put(("grab_fail", shot, when, None))
                                continue
                            verdict = self._ask(
                                host, model, jpeg, prompt, num_ctx, num_gpu
                            )
                            fails = 0
                            result_q.put(("ok", shot, when, verdict))
                        except (urllib.error.URLError, TimeoutError, OSError) as exc:
                            fails += 1
                            if attempt + 1 < max_attempts:
                                work_q.put((shot, when, attempt + 1))  # peer tries
                            else:
                                result_q.put(("give_up", shot, when, exc))
                            if fails >= 5:
                                # This host is persistently down: evict it and let
                                # the survivors carry the run. Its in-flight sample
                                # was already requeued (or given up) above.
                                result_q.put(("host_dead", host, None, exc))
                                break
                        except Exception as exc:  # noqa: BLE001
                            # Any other error (a frame-grab/decode failure, etc.)
                            # must not strand the consumer. Resolve this sample as
                            # a give-up — counted as done for the loop, left
                            # un-done on disk so a resume retries it — and carry on.
                            result_q.put(("give_up", shot, when, exc))
                finally:
                    if is_local:
                        _local_host_worker_stopped()
                    with state_lock:
                        live -= 1
                        if live == 0:
                            result_q.put(("drained", None, None, None))

            threads = [
                threading.Thread(
                    target=worker, args=(host,), daemon=True, name=f"vlm-{host}"
                )
                for host in hosts
                for _ in range(per_host)
            ]
            for t in threads:
                t.start()

            resolved = 0
            dead = 0
            last_exc: Exception | None = None
            try:
                while resolved < len(pending):
                    kind, a, b, extra = result_q.get()
                    if kind == "drained":
                        break  # every worker exited; if under target, all died
                    if kind == "host_dead":
                        dead += 1
                        last_exc = extra
                        progress(
                            0.05 + 0.93 * (already + resolved) / len(plan),
                            f"vision host {a} stopped responding "
                            f"({dead}/{len(hosts)} down)",
                        )
                        continue
                    resolved += 1
                    if kind == "ok":
                        fold(a, b, extra)
                    elif kind == "give_up":
                        last_exc = extra  # left un-done; a resume retries it
                    # "grab_fail": frame unreadable — skip, as the serial path did
                    completed = already + resolved
                    if completed % 25 == 0 or completed == len(plan):
                        save_checkpoint()
                        progress(
                            0.05 + 0.93 * completed / len(plan),
                            f"{completed}/{len(plan)} samples, "
                            f"{len(hits)} shots flagged",
                        )
            finally:
                stop.set()
                for t in threads:
                    t.join(timeout=5)
                save_checkpoint()

            if resolved < len(pending):
                # Every worker exited before the queue drained: no host is
                # answering. The checkpoint holds all progress, so a rerun
                # resumes exactly here.
                raise RuntimeError(
                    f"all {len(hosts)} vision host(s) unresponsive; progress "
                    f"saved — rerun to resume: {last_exc}"
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
