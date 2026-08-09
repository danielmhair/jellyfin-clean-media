"""Batch review state for a page of the library.

This is what the Jellyfin review grid renders from, so the counts must
match what a reviewer would see on opening the film.
"""

import json

import pytest
from fastapi.testclient import TestClient

from worker.main import app
from worker.models import Segment, Timeline
from worker.review import sidecar_for


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point path resolution at a throwaway library rather than movies/
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    return TestClient(app)


def _film(tmp_path, name, segments=None):
    media = tmp_path / name
    media.write_bytes(b"pretend video bytes")
    if segments is not None:
        sidecar_for(media).write_text(
            json.dumps(Timeline(mediaFingerprint="fp", segments=segments).model_dump()),
            encoding="utf-8",
        )
    return media


def _segment(id, approved=None):
    return Segment(id=id, startMs=id * 1000, endMs=id * 1000 + 500,
                   category="profanity", confidence=1.0, engine="subtitles",
                   recommendedAction="mute", approved=approved)


def test_unanalyzed_film_reports_nothing_to_review(client, tmp_path):
    _film(tmp_path, "Fresh.mkv")

    body = client.post("/api/status", json={"paths": ["Fresh.mkv"]}).json()

    assert body[0]["analyzed"] is False
    assert (body[0]["total"], body[0]["pending"]) == (0, 0)
    assert body[0]["resolvedPath"] is not None


def test_partly_reviewed_film_reports_what_is_left(client, tmp_path):
    _film(tmp_path, "Partly.mkv", [
        _segment(1, approved=True),
        _segment(2, approved=False),
        _segment(3),
        _segment(4),
    ])

    body = client.post("/api/status", json={"paths": ["Partly.mkv"]}).json()

    assert body[0]["analyzed"] is True
    assert body[0]["total"] == 4
    assert body[0]["approved"] == 1
    assert body[0]["rejected"] == 1
    assert body[0]["pending"] == 2


def test_fully_reviewed_film_has_nothing_pending(client, tmp_path):
    _film(tmp_path, "Done.mkv", [_segment(1, approved=True), _segment(2, approved=False)])

    body = client.post("/api/status", json={"paths": ["Done.mkv"]}).json()

    assert body[0]["pending"] == 0
    assert (body[0]["approved"], body[0]["rejected"]) == (1, 1)


def test_unknown_path_is_reported_not_an_error(client):
    """One missing film must not fail the whole page of the grid."""
    response = client.post("/api/status", json={"paths": ["Nowhere.mkv"]})

    assert response.status_code == 200
    assert response.json()[0]["resolvedPath"] is None
    assert response.json()[0]["analyzed"] is False


def test_results_follow_request_order(client, tmp_path):
    """The plugin zips ids onto results positionally."""
    _film(tmp_path, "A.mkv", [_segment(1, approved=True)])
    _film(tmp_path, "B.mkv")
    _film(tmp_path, "C.mkv", [_segment(1), _segment(2)])

    paths = ["C.mkv", "Nowhere.mkv", "A.mkv", "B.mkv"]
    body = client.post("/api/status", json={"paths": paths}).json()

    assert len(body) == len(paths)
    assert [b["path"] for b in body] == paths
    assert body[0]["pending"] == 2
    assert body[2]["approved"] == 1


def test_foreign_mount_paths_resolve_by_name(client, tmp_path):
    """Jellyfin knows the film by its own mount path, not the worker's."""
    _film(tmp_path, "Iron Man (2008).mkv", [_segment(1, approved=True)])

    body = client.post(
        "/api/status",
        json={"paths": ["/volume1/Media/Movies/Iron Man (2008).mkv"]},
    ).json()

    assert body[0]["analyzed"] is True
    assert body[0]["approved"] == 1


def test_running_analysis_is_reported_alongside_counts(client, tmp_path, monkeypatch):
    from worker.main import store
    from worker.models import Job, JobStatus

    media = _film(tmp_path, "Analyzing.mkv")
    store.save_job(Job(id="job1", mediaPath=str(media), engine="vlm",
                       status=JobStatus.running, progress=0.42, stage="sampling shots"))

    body = client.post("/api/status", json={"paths": ["Analyzing.mkv"]}).json()

    assert body[0]["job"]["status"] == "running"
    assert body[0]["job"]["progress"] == 0.42
    assert body[0]["job"]["stage"] == "sampling shots"

    store.delete_job("job1")


def test_running_pass_is_not_hidden_behind_a_queued_one(client, tmp_path):
    """Two engines on one film: a running visual pass must be the headline,
    even when a profanity pass was queued a moment later (same file name).
    The grid read the newest-created job, showing 'queued' while a multi-hour
    pass was already running."""
    from worker.main import store
    from worker.models import Job, JobStatus

    media = _film(tmp_path, "Two Engines.mkv")
    # vlm created first, whisper queued just after — newest-first ordering
    # would otherwise pick the queued whisper.
    store.save_job(Job(id="vlm1", mediaPath=str(media), engine="vlm",
                       status=JobStatus.running, progress=0.45, stage="2300/5404 samples"))
    store.save_job(Job(id="whis1", mediaPath=str(media), engine="whisper",
                       status=JobStatus.queued))

    body = client.post("/api/status", json={"paths": ["Two Engines.mkv"]}).json()

    assert body[0]["job"]["status"] == "running"
    assert body[0]["job"]["engine"] == "vlm"
    assert body[0]["job"]["progress"] == 0.45
    # Both in-flight passes are reported, running first, each with its engine.
    assert [(j["engine"], j["status"]) for j in body[0]["jobs"]] == [
        ("vlm", "running"), ("whisper", "queued")
    ]

    store.delete_job("vlm1")
    store.delete_job("whis1")


def test_finished_engines_are_reported(client, tmp_path):
    """The UI stops offering an engine that already ran; a render job is not an
    analysis engine and must not appear among them."""
    from worker.main import store
    from worker.models import Job, JobStatus

    media = _film(tmp_path, "Done Engines.mkv")
    store.save_job(Job(id="d1", mediaPath=str(media), engine="subtitles",
                       status=JobStatus.completed))
    store.save_job(Job(id="d2", mediaPath=str(media), engine="vlm",
                       status=JobStatus.running, progress=0.1))
    store.save_job(Job(id="d3", mediaPath=str(media), engine="render",
                       status=JobStatus.rendered))

    body = client.post("/api/status", json={"paths": ["Done Engines.mkv"]}).json()

    assert body[0]["enginesDone"] == ["subtitles"]  # not vlm (running), not render

    for jid in ("d1", "d2", "d3"):
        store.delete_job(jid)


def test_empty_request_is_answered_empty(client):
    assert client.post("/api/status", json={"paths": []}).json() == []
