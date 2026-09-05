"""The persisted-order job queue: reorder, requeue, pause, and run order.

These drive the real :class:`JobQueue` (the same injectable harness
``tests/test_recovery.py`` uses) and assert on observable behaviour — the order
the recording engine actually runs jobs in, and the persisted job rows — never
on private fields. The one structural change this feature makes (FIFO →
persisted ``queuePosition``) lives here, so this is where it is pinned down.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from worker.engines.base import EngineAdapter
from worker.models import Job, JobCreate, JobStatus, Timeline
from worker.cleancopy import (
    clean_output_path,
    next_clean_output_path,
    render_plan,
)
from worker.queue import JobQueue
from worker.store import Store

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _RecordingEngine(EngineAdapter):
    """Records the order films are analysed in, so a test can assert on it."""

    name = "fake"
    resumable = False

    def __init__(self) -> None:
        self.order: list[str] = []
        self.calls = 0

    def version(self) -> str:
        return "1.0"

    def health(self):
        return {}

    def capabilities(self):
        return {}

    def analyze(self, media_path, fingerprint, options, progress):
        self.calls += 1
        self.order.append(Path(media_path).name)
        progress(1.0, "done")
        return Timeline(mediaFingerprint=fingerprint, segments=[]), None

    def render(self, *args, **kwargs):
        raise NotImplementedError


class _ConcurrencyEngine(EngineAdapter):
    """Blocks inside analyze() until released, tracking how many calls are in
    flight at once — so a test can assert on overlap (or its absence) between
    lanes, not just eventual completion order."""

    resumable = False

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.release = threading.Event()

    def version(self) -> str:
        return "1.0"

    def health(self):
        return {}

    def capabilities(self):
        return {}

    def analyze(self, media_path, fingerprint, options, progress):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.release.wait(timeout=5.0)
        finally:
            with self._lock:
                self.active -= 1
        progress(1.0, "done")
        return Timeline(mediaFingerprint=fingerprint, segments=[]), None

    def render(self, *args, **kwargs):
        raise NotImplementedError


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def engine(monkeypatch):
    import worker.queue as queue_mod

    eng = _RecordingEngine()
    monkeypatch.setitem(queue_mod.ENGINES, "fake", eng)
    return eng


def _media(tmp_path, i):
    """A distinct throwaway media file (distinct bytes → distinct fingerprint)."""
    media = tmp_path / f"Film {i}.mkv"
    media.write_bytes(b"x" * 4096 + str(i).encode())
    return media


def _save(store, tmp_path, i, status=JobStatus.queued, engine="fake",
          position=None, error=None, progress=0.0):
    """Write a job row straight to the store, as a prior run would have left it."""
    media = _media(tmp_path, i)
    job = Job(
        id=f"job{i}",
        mediaPath=str(media),
        engine=engine,
        mediaFingerprint=f"fp{i}",
        status=status,
        progress=progress,
        error=error,
        queuePosition=position,
        createdAt=BASE + timedelta(seconds=i),
    )
    store.save_job(job)
    return job


# -- submit assigns positions -------------------------------------------------


def test_submit_assigns_increasing_positions(engine, tmp_path):
    # Window closed, so nothing runs and the positions can be inspected at rest.
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: False, poll_s=0.01)

    ids = [
        q.submit(JobCreate(mediaPath=str(_media(tmp_path, i)), engine="fake")).id
        for i in range(3)
    ]

    positions = [store.get_job(jid).queuePosition for jid in ids]
    assert positions == [0, 1, 2]  # each submission goes to the back


# -- reorder ------------------------------------------------------------------


def test_reorder_changes_run_order(engine, tmp_path):
    """Queue several jobs outside the window, reorder, open the window — the
    engine runs them in the NEW order, because the worker never commits to a job
    while it waits."""
    gate = {"open": False}
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: gate["open"], poll_s=0.01)

    a = q.submit(JobCreate(mediaPath=str(_media(tmp_path, 0)), engine="fake"))
    b = q.submit(JobCreate(mediaPath=str(_media(tmp_path, 1)), engine="fake"))
    c = q.submit(JobCreate(mediaPath=str(_media(tmp_path, 2)), engine="fake"))
    # Nothing may run yet.
    time.sleep(0.1)
    assert engine.order == []

    q.reorder([c.id, a.id, b.id])
    gate["open"] = True

    assert _wait(lambda: len(engine.order) == 3)
    assert engine.order == ["Film 2.mkv", "Film 0.mkv", "Film 1.mkv"]


def test_reorder_ignores_running_terminal_and_unknown_ids(engine, tmp_path):
    """Reorder repositions only queued/rendering jobs; a running, terminal or
    unknown id is left exactly where it was."""
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: False, poll_s=0.01)

    _save(store, tmp_path, 1, status=JobStatus.queued, position=0)
    _save(store, tmp_path, 2, status=JobStatus.running, position=5)
    _save(store, tmp_path, 3, status=JobStatus.completed, position=9)

    q.reorder(["job3", "job2", "nope", "job1"])

    assert store.get_job("job1").queuePosition == 0  # only pending id repositioned
    assert store.get_job("job2").queuePosition == 5  # running untouched
    assert store.get_job("job3").queuePosition == 9  # terminal untouched


# -- requeue ------------------------------------------------------------------


def test_requeue_failed_job_reruns_with_error_cleared(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    _save(store, tmp_path, 0, status=JobStatus.failed, error="no subtitle track")
    q = JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)

    job = q.requeue("job0")
    assert job.status == JobStatus.queued
    assert job.error is None

    assert _wait(lambda: store.get_job("job0").status == JobStatus.completed)
    assert engine.calls == 1  # it actually re-ran


def test_requeue_sends_the_job_to_the_back(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    _save(store, tmp_path, 0, status=JobStatus.queued, position=0)  # already waiting
    _save(store, tmp_path, 1, status=JobStatus.failed, position=1)
    q = JobQueue(store, allowed_fn=lambda now: False, poll_s=0.01)

    job = q.requeue("job1")
    # Fresh end-of-queue position: above every position now in the store.
    assert job.queuePosition > store.get_job("job0").queuePosition


def test_requeue_is_refused_for_running_or_queued(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: False, poll_s=0.01)
    # Saved AFTER construction so recovery does not reset the running one.
    _save(store, tmp_path, 0, status=JobStatus.queued)
    _save(store, tmp_path, 1, status=JobStatus.running)

    with pytest.raises(ValueError):
        q.requeue("job0")
    with pytest.raises(ValueError):
        q.requeue("job1")
    with pytest.raises(KeyError):
        q.requeue("missing")


# -- pause / resume -----------------------------------------------------------


def test_pause_holds_the_next_start_and_resume_releases_it(engine, tmp_path):
    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)
    q.set_paused(True)

    q.submit(JobCreate(mediaPath=str(_media(tmp_path, 0)), engine="fake"))
    time.sleep(0.2)
    assert engine.order == []  # paused: nothing starts

    q.set_paused(False)
    assert _wait(lambda: engine.order == ["Film 0.mkv"])  # resume releases it


# -- lanes: vlm runs alongside general -----------------------------------------


def test_vlm_lane_runs_concurrently_with_general_lane(tmp_path, monkeypatch):
    """A whisper job and a vlm job both start without waiting on each other --
    proving the two lanes are independent, not one worker sharing turns."""
    import worker.queue as queue_mod

    vlm = _ConcurrencyEngine("vlm")
    whisper = _ConcurrencyEngine("whisper")
    monkeypatch.setitem(queue_mod.ENGINES, "vlm", vlm)
    monkeypatch.setitem(queue_mod.ENGINES, "whisper", whisper)

    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)
    q.submit(JobCreate(mediaPath=str(_media(tmp_path, 0)), engine="vlm"))
    q.submit(JobCreate(mediaPath=str(_media(tmp_path, 1)), engine="whisper"))

    assert _wait(lambda: vlm.active == 1 and whisper.active == 1)

    vlm.release.set()
    whisper.release.set()
    assert _wait(lambda: vlm.active == 0 and whisper.active == 0)


def test_two_vlm_jobs_still_run_one_at_a_time(tmp_path, monkeypatch):
    """The vlm lane keeps its single-slot behaviour -- a second visual job
    waits for the first, it doesn't split hosts or run alongside it."""
    import worker.queue as queue_mod

    vlm = _ConcurrencyEngine("vlm")
    monkeypatch.setitem(queue_mod.ENGINES, "vlm", vlm)

    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)
    q.submit(JobCreate(mediaPath=str(_media(tmp_path, 0)), engine="vlm"))
    second = q.submit(JobCreate(mediaPath=str(_media(tmp_path, 1)), engine="vlm"))

    assert _wait(lambda: vlm.active == 1)
    time.sleep(0.2)  # give a (buggy) second slot a chance to also claim it
    assert vlm.active == 1
    assert store.get_job(second.id).status == JobStatus.queued

    vlm.release.set()
    assert _wait(lambda: store.get_job(second.id).status == JobStatus.completed)
    assert vlm.max_active == 1


def test_two_general_lane_jobs_still_run_one_at_a_time(tmp_path, monkeypatch):
    """The general lane is unchanged too -- two whisper jobs still serialize
    (this stays true even though it's not a requirement, just a side effect
    of the general lane keeping its single-slot behaviour)."""
    import worker.queue as queue_mod

    whisper = _ConcurrencyEngine("whisper")
    monkeypatch.setitem(queue_mod.ENGINES, "whisper", whisper)

    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)
    q.submit(JobCreate(mediaPath=str(_media(tmp_path, 0)), engine="whisper"))
    second = q.submit(JobCreate(mediaPath=str(_media(tmp_path, 1)), engine="whisper"))

    assert _wait(lambda: whisper.active == 1)
    time.sleep(0.2)
    assert whisper.active == 1
    assert store.get_job(second.id).status == JobStatus.queued

    whisper.release.set()
    assert _wait(lambda: store.get_job(second.id).status == JobStatus.completed)
    assert whisper.max_active == 1


def test_whisper_waits_while_this_machines_gpu_is_busy_with_a_visual_pass(
    tmp_path, monkeypatch
):
    """Whisper loads its own CUDA model on this same machine, so it must not
    start while ``vlm_engine`` reports this machine's own GPU is actively
    driving a running visual pass -- that would fight the local Ollama host
    for the one card instead of genuinely running in parallel. A different
    general-lane job that doesn't touch the GPU is unaffected."""
    import worker.queue as queue_mod
    from worker.engines import vlm_engine as vlm_engine_mod

    whisper = _RecordingEngine()
    other = _RecordingEngine()
    monkeypatch.setitem(queue_mod.ENGINES, "whisper", whisper)
    monkeypatch.setitem(queue_mod.ENGINES, "subtitles", other)
    monkeypatch.setattr(vlm_engine_mod, "local_host_busy", lambda: True)

    store = Store(db_path=tmp_path / "jobs.db")
    q = JobQueue(store, allowed_fn=lambda now: True, poll_s=0.01)
    whisper_job = q.submit(
        JobCreate(mediaPath=str(_media(tmp_path, 0)), engine="whisper")
    )
    q.submit(JobCreate(mediaPath=str(_media(tmp_path, 1)), engine="subtitles"))

    # The GPU-free general-lane job goes ahead; whisper holds.
    assert _wait(lambda: other.calls == 1)
    time.sleep(0.2)
    assert whisper.calls == 0
    held = store.get_job(whisper_job.id)
    assert held.status == JobStatus.queued
    assert held.stage == "waiting for the visual pass to free this machine's GPU"

    # Once the visual pass frees this machine's GPU, whisper is released.
    monkeypatch.setattr(vlm_engine_mod, "local_host_busy", lambda: False)
    q._signal()
    assert _wait(lambda: whisper.calls == 1)


# -- where the clean copy is written ------------------------------------------
# Jellyfin only groups files as selectable *versions* of one movie when they
# share a per-movie folder and each name begins, character for character, with
# the folder name + " - <label>". Get this wrong and a clean copy shows up as a
# duplicate movie, so the naming is pinned here.


def test_clean_copy_is_a_version_when_the_film_has_its_own_folder(tmp_path):
    folder = tmp_path / "Guardians of the Galaxy (2014)"
    folder.mkdir()
    media = folder / "Guardians of the Galaxy (2014).mkv"
    out = clean_output_path(media)
    # Same folder, folder-name prefix + " - Clean": Jellyfin reads it as a version.
    assert out == folder / "Guardians of the Galaxy (2014) - Clean.mkv"
    assert out.parent == media.parent


def test_clean_copy_version_prefix_includes_provider_ids(tmp_path):
    folder = tmp_path / "Movie (2021) [imdbid-tt12801262]"
    folder.mkdir()
    media = folder / "Movie (2021) [imdbid-tt12801262].mkv"
    # The prefix must match the folder name exactly, provider ids and all.
    assert clean_output_path(media) == folder / "Movie (2021) [imdbid-tt12801262] - Clean.mkv"


def test_clean_copy_falls_back_to_subfolder_for_a_flat_library(tmp_path):
    # A film that isn't in its own folder can't be a version — writing a file
    # named after the shared parent would mis-group — so use a safe subfolder.
    media = tmp_path / "Guardians of the Galaxy (2014).mkv"
    out = clean_output_path(media)
    assert out == tmp_path / "cleaned" / "Guardians of the Galaxy (2014) (Clean).mkv"


def test_clean_copy_falls_back_for_an_episode_under_a_season_folder(tmp_path):
    season = tmp_path / "Show" / "Season 01"
    season.mkdir(parents=True)
    media = season / "Show - S01E01.mkv"
    out = clean_output_path(media)
    assert out == season / "cleaned" / "Show - S01E01 (Clean).mkv"


# -- re-rendering from a clean copy -------------------------------------------
# Reviewing the clean copy (not the original) is how a missed word is normally
# found, so a clean copy is a routine *input* to the next render. It has to stay
# in the same version family, and it must not be clobbered without being asked.


def _film(tmp_path):
    folder = tmp_path / "Some Film (2010)"
    folder.mkdir()
    media = folder / "Some Film (2010).mkv"
    media.write_bytes(b"original")
    return folder, media


def test_re_rendering_a_clean_copy_targets_the_same_version_not_a_nested_one(tmp_path):
    folder, _ = _film(tmp_path)
    clean = folder / "Some Film (2010) - Clean.mkv"
    assert clean_output_path(clean) == clean  # not "Clean (Clean)", not cleaned/


def test_a_kept_alongside_copy_is_its_own_jellyfin_version(tmp_path):
    folder, media = _film(tmp_path)
    assert clean_output_path(media, 2) == folder / "Some Film (2010) - Clean 2.mkv"
    # And numbering continues from a numbered copy, rather than restarting.
    numbered = folder / "Some Film (2010) - Clean 2.mkv"
    assert clean_output_path(numbered, 3) == folder / "Some Film (2010) - Clean 3.mkv"


def test_next_clean_output_path_skips_the_copies_already_on_disk(tmp_path):
    folder, media = _film(tmp_path)
    assert next_clean_output_path(media) == folder / "Some Film (2010) - Clean.mkv"
    (folder / "Some Film (2010) - Clean.mkv").write_bytes(b"c1")
    (folder / "Some Film (2010) - Clean 2.mkv").write_bytes(b"c2")
    assert next_clean_output_path(media) == folder / "Some Film (2010) - Clean 3.mkv"


def test_render_plan_from_the_original_replaces_the_existing_clean_copy(tmp_path):
    folder, media = _film(tmp_path)
    (folder / "Some Film (2010) - Clean.mkv").write_bytes(b"c1")
    plan = render_plan(media)
    assert plan["sourceIsCleanCopy"] is False
    assert plan["replacePath"] == str(folder / "Some Film (2010) - Clean.mkv")
    assert plan["replaceExists"] is True
    assert plan["newPath"] == str(folder / "Some Film (2010) - Clean 2.mkv")
    assert (plan["replaceLabel"], plan["newLabel"]) == ("Clean", "Clean 2")


def test_render_plan_from_a_clean_copy_replaces_that_copy(tmp_path):
    folder, _ = _film(tmp_path)
    clean = folder / "Some Film (2010) - Clean.mkv"
    clean.write_bytes(b"c1")
    plan = render_plan(clean)
    assert plan["sourceIsCleanCopy"] is True
    assert plan["replacePath"] == str(clean)      # the copy being watched
    assert plan["newPath"] == str(folder / "Some Film (2010) - Clean 2.mkv")


def test_an_episode_in_a_flat_show_folder_is_not_mistaken_for_a_version(tmp_path):
    # "Show - S01E01.mkv" is folder-prefixed too. Reading that as a version
    # label would collide every episode onto one "Show - Clean.mkv".
    show = tmp_path / "Show"
    show.mkdir()
    media = show / "Show - S01E01.mkv"
    assert clean_output_path(media) == show / "cleaned" / "Show - S01E01 (Clean).mkv"


def test_a_legacy_cleaned_copy_re_renders_as_a_sibling_not_a_nest(tmp_path):
    media = tmp_path / "cleaned" / "Some Film (2010) (Clean).mkv"
    assert clean_output_path(media) == media
    assert clean_output_path(media, 2) == media.with_name("Some Film (2010) (Clean 2).mkv")
