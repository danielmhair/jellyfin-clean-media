"""The additive job endpoints: reorder, requeue, and pause.

Driven through the real FastAPI app (like ``tests/test_status.py``), asserting on
the responses and the resulting job rows — these routes are thin over the
``JobQueue`` methods that ``tests/test_queue.py`` pins down, so here we only
check the HTTP surface: shape, persistence, and the error cases.

The app's module-level queue runs a live worker thread against the throwaway
test DB (see ``tests/conftest.py``). To keep it from actually running the jobs
these tests plant, the queue is paused for the duration and the jobs are removed
before it is resumed.
"""

import pytest
from fastapi.testclient import TestClient

from worker.main import app, jobs, store
from worker.models import Job, JobStatus


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def paused_queue():
    """Hold the worker so planted jobs are never actually run, and clean up."""
    jobs.set_paused(True)
    planted: list[str] = []
    yield planted
    for job_id in planted:
        store.delete_job(job_id)
    jobs.set_paused(False)


def _plant(planted, job_id, status=JobStatus.queued, engine="subtitles",
           position=None, error=None):
    job = Job(id=job_id, mediaPath=f"/m/{job_id}.mkv", engine=engine,
              status=status, error=error, queuePosition=position)
    store.save_job(job)
    planted.append(job_id)
    return job


# -- reorder ------------------------------------------------------------------


def test_reorder_repositions_queued_jobs(client, paused_queue):
    _plant(paused_queue, "r1", position=0)
    _plant(paused_queue, "r2", position=1)

    resp = client.post("/api/jobs/reorder", json={"ids": ["r2", "r1"]})

    assert resp.status_code == 200
    positions = {j["id"]: j["queuePosition"] for j in resp.json()}
    assert positions["r2"] == 0 and positions["r1"] == 1
    # And it persisted, not just echoed.
    assert store.get_job("r2").queuePosition == 0


# -- requeue ------------------------------------------------------------------


def test_requeue_failed_job_returns_it_queued(client, paused_queue):
    _plant(paused_queue, "f1", status=JobStatus.failed, error="boom")

    resp = client.post("/api/jobs/f1/requeue")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["error"] is None
    assert body["queuePosition"] is not None
    assert store.get_job("f1").status == JobStatus.queued


def test_requeue_unknown_job_is_404(client, paused_queue):
    assert client.post("/api/jobs/nope/requeue").status_code == 404


def test_requeue_of_a_completed_job_is_400(client, paused_queue):
    _plant(paused_queue, "c1", status=JobStatus.completed)

    assert client.post("/api/jobs/c1/requeue").status_code == 400


# -- pause / resume -----------------------------------------------------------


def test_pause_and_resume_reflected_in_health(client):
    try:
        assert client.post("/api/jobs/pause", json={"paused": True}).json()["paused"] is True
        assert client.get("/api/health").json()["paused"] is True

        assert client.post("/api/jobs/pause", json={"paused": False}).json()["paused"] is False
        assert client.get("/api/health").json()["paused"] is False
    finally:
        jobs.set_paused(False)  # never leave the shared queue paused
