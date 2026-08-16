"""Multi-host dispatch for the visual pass.

The VLM engine fans one film's samples across a pool of Ollama hosts so two
machines' GPUs work the same pass at once. These tests stub ``_grab``/``_ask``
so no ffmpeg or GPU is needed, and drive a synthetic shot list through
``analyze`` to check the pool balances, fails over, and aborts cleanly.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter

import pytest

from worker.engines.vlm_engine import VLMEngine
from worker.shots import Shot, sample_times, save_shots


def _make_film(tmp_path, n_shots=40):
    media = tmp_path / "Some Film (2010).mkv"
    media.write_bytes(b"not a real video")
    shots = [Shot(i, i * 24, (i + 1) * 24, float(i), float(i + 1)) for i in range(n_shots)]
    cache = tmp_path / "shots.json"
    save_shots(shots, cache)
    plan = sum(len(sample_times(s, 2.5, 1)) for s in shots)
    return media, cache, plan


def _run(engine, media, cache, hosts, **extra):
    opts = {"shots": str(cache), "hosts": hosts, "minSamples": 1, **extra}
    return engine.analyze(media, "fp", opts, lambda frac, stage: None)


def test_samples_fan_out_across_both_hosts(tmp_path, monkeypatch):
    media, cache, plan = _make_film(tmp_path)
    eng = VLMEngine()
    monkeypatch.setattr(eng, "_grab", lambda m, when: b"jpeg")

    seen = Counter()
    lock = threading.Lock()

    def fake_ask(host, model, jpeg, prompt, num_ctx=2048, num_gpu=None):
        # A real inference takes seconds; without a little latency here the
        # first-started worker drains the whole queue before its peer wakes.
        time.sleep(0.01)
        with lock:
            seen[host] += 1
        return {}  # nothing detected

    monkeypatch.setattr(eng, "_ask", fake_ask)
    hosts = ["http://a:11434", "http://b:11434"]
    timeline, _ = _run(eng, media, cache, hosts)

    # Every sample answered exactly once, and both GPUs did real work.
    assert sum(seen.values()) == plan
    assert set(seen) == set(hosts)
    assert seen["http://a:11434"] > 0 and seen["http://b:11434"] > 0
    assert timeline.segments == []


def test_detections_survive_concurrent_dispatch(tmp_path, monkeypatch):
    media, cache, _ = _make_film(tmp_path)
    eng = VLMEngine()
    monkeypatch.setattr(eng, "_grab", lambda m, when: b"jpeg")
    monkeypatch.setattr(
        eng,
        "_ask",
        lambda *a, **k: {"female_topless": True, "description": "x"},
    )
    timeline, _ = _run(eng, media, cache, ["http://a:11434", "http://b:11434"])
    # Concurrency must not lose findings: every shot flagged → one scene.
    assert len(timeline.segments) >= 1
    assert timeline.segments[0].category == "nudity"


def test_tie_break_is_earliest_frame_not_arrival_order(tmp_path, monkeypatch):
    """A shot flagged by several of its frames is labelled by the earliest.

    Different frames of one shot can be answered out of order across the pool,
    so the winning description must be chosen by timestamp, not arrival.
    """
    media = tmp_path / "Some Film (2010).mkv"
    media.write_bytes(b"x")
    # One long shot → several samples, all at the same (nudity) severity.
    shot = Shot(0, 0, 240, 0.0, 10.0)
    cache = tmp_path / "shots.json"
    save_shots([shot], cache)
    times = sample_times(shot, 2.5, 1)
    assert len(times) > 1  # the tie only exists with multiple frames per shot

    eng = VLMEngine()
    # Carry each frame's timestamp through _grab so _ask can label it, and make
    # the latest frame answer first (longest sleep for the smallest time).
    monkeypatch.setattr(eng, "_grab", lambda m, when: repr(when).encode())

    hi = max(times)

    def fake_ask(host, model, jpeg, prompt, num_ctx=2048, num_gpu=None):
        when = float(eval(jpeg.decode()))
        time.sleep(0.02 * (hi - when + 0.01))  # earliest frame finishes last
        return {"female_topless": True, "description": f"frame@{when:.2f}"}

    monkeypatch.setattr(eng, "_ask", fake_ask)
    timeline, _ = _run(
        eng, media, cache, ["http://a:11434", "http://b:11434"], minSamples=1
    )
    assert len(timeline.segments) == 1
    # The reasoning carries the winning frame's description; it must be the
    # earliest timestamp even though that frame's answer arrived last.
    assert f"frame@{min(times):.2f}" in timeline.segments[0].reasoning


def test_one_dead_host_fails_over_to_the_survivor(tmp_path, monkeypatch):
    media, cache, plan = _make_film(tmp_path)
    eng = VLMEngine()
    monkeypatch.setattr(eng, "_grab", lambda m, when: b"jpeg")

    good = Counter()
    lock = threading.Lock()

    def fake_ask(host, model, jpeg, prompt, num_ctx=2048, num_gpu=None):
        if host.endswith("dead:11434"):
            raise TimeoutError("host down")
        with lock:
            good[host] += 1
        return {}

    monkeypatch.setattr(eng, "_ask", fake_ask)
    # A dead machine must not abort the run — the survivor carries it.
    timeline, _ = _run(
        eng, media, cache, ["http://dead:11434", "http://good:11434"]
    )
    assert timeline is not None
    assert set(good) == {"http://good:11434"}  # only the live host ever succeeded
    # Nearly everything got done on the survivor; a few early samples may be
    # left for a resume, but the run itself completed without raising.
    assert good["http://good:11434"] >= plan - 10


def test_worker_exception_does_not_wedge_the_pass(tmp_path, monkeypatch):
    """An unexpected error in a worker must not hang the consumer.

    Regression: `_grab` (frame extraction) sat outside the worker's try, so a
    decode/subprocess error killed the thread before it decremented the live
    count or posted "drained" — and the consumer blocked forever on the result
    queue, freezing the whole pass. Every sample here raises a non-network
    error; the run must still finish (as give-ups), not hang.
    """
    media, cache, _ = _make_film(tmp_path, n_shots=20)
    eng = VLMEngine()

    def boom(m, when):
        raise ValueError("frame decode blew up")

    monkeypatch.setattr(eng, "_grab", boom)
    monkeypatch.setattr(eng, "_ask", lambda *a, **k: {})  # never reached

    result = {}
    err = {}

    def run():
        try:
            result["tl"], _ = _run(eng, media, cache, ["http://a:11434", "http://b:11434"])
        except Exception as e:  # noqa: BLE001 — surface, don't swallow, in the test
            err["e"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=30)
    assert not th.is_alive(), "analyze() hung — a worker died without draining"
    # It resolved every sample as a give-up: no crash, no findings, no hang.
    assert "e" not in err, f"unexpected raise: {err.get('e')}"
    assert result["tl"].segments == []


def test_one_host_raising_unexpectedly_still_completes_on_the_other(tmp_path, monkeypatch):
    """A non-network fault on one host fails that sample over, run completes."""
    media, cache, _ = _make_film(tmp_path, n_shots=20)
    eng = VLMEngine()
    monkeypatch.setattr(eng, "_grab", lambda m, when: b"jpeg")

    def fake_ask(host, model, jpeg, prompt, num_ctx=2048, num_gpu=None):
        if host.endswith("bad:11434"):
            # Slow, so it can't drain the shared queue into give-ups before the
            # good host works it — an unexpected error isn't retried on a peer.
            time.sleep(0.05)
            raise ValueError("boom")  # not URLError/TimeoutError/OSError
        return {"female_topless": True, "description": "x"}

    monkeypatch.setattr(eng, "_ask", fake_ask)
    result = {}

    def run():
        result["tl"], _ = _run(eng, media, cache, ["http://bad:11434", "http://good:11434"])

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=30)
    assert not th.is_alive(), "analyze() hung"
    # The good host's detections survived; the run did not wedge on the bad one.
    assert len(result["tl"].segments) >= 1


def test_all_hosts_down_aborts_with_checkpoint(tmp_path, monkeypatch):
    media, cache, _ = _make_film(tmp_path)
    eng = VLMEngine()
    monkeypatch.setattr(eng, "_grab", lambda m, when: b"jpeg")

    def fake_ask(*a, **k):
        raise TimeoutError("host down")

    monkeypatch.setattr(eng, "_ask", fake_ask)
    with pytest.raises(RuntimeError, match="unresponsive"):
        _run(eng, media, cache, ["http://a:11434"])

    # Progress is checkpointed so a rerun resumes rather than restarts.
    checkpoint = media.with_name(media.stem + ".vlm-progress.json")
    assert checkpoint.exists()
    json.loads(checkpoint.read_text(encoding="utf-8"))  # valid JSON


def test_hosts_env_default(monkeypatch):
    eng = VLMEngine()
    monkeypatch.setenv("CLEANMEDIA_VLM_HOSTS", "http://x:11434, http://y:11434/")
    # No explicit host/hosts in options → the env pool is used, trimmed.
    assert eng._hosts({}) == ["http://x:11434", "http://y:11434"]
    # An explicit single host still wins over the env pool.
    assert eng._hosts({"host": "http://z:11434"}) == ["http://z:11434"]
    # An explicit hosts list wins outright and de-dupes.
    assert eng._hosts({"hosts": ["http://a", "http://a", "http://b"]}) == [
        "http://a",
        "http://b",
    ]
