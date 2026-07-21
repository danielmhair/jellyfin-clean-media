"""Editing findings: retime, delete, add by hand, and survive re-analysis.

These cover the review UI's write path. A bug here silently discards an
administrator's decisions, which is the one failure the approval gate
exists to prevent.
"""

import json

import pytest
from fastapi.testclient import TestClient

from worker.main import app
from worker.models import Segment, Timeline
from worker.review import (
    MANUAL_ENGINE,
    create_segment,
    delete_segment,
    load_timeline,
    merge_segments,
    sidecar_for,
    update_segment,
)


def _film(tmp_path, segments=None):
    media = tmp_path / "Film.mkv"
    media.write_bytes(b"not really a video, but it has a size")
    timeline = Timeline(
        mediaFingerprint="fp",
        segments=segments
        if segments is not None
        else [
            Segment(id=1, startMs=1000, endMs=2000, category="nudity",
                    confidence=0.9, engine="vlm", recommendedAction="skip",
                    engineRef="shot-4"),
            Segment(id=2, startMs=5000, endMs=5800, category="profanity",
                    confidence=1.0, engine="subtitles", recommendedAction="mute",
                    engineRef="cue-11"),
        ],
    )
    sidecar_for(media).write_text(json.dumps(timeline.model_dump()), encoding="utf-8")
    return media


def test_retiming_persists_and_leaves_other_fields_alone(tmp_path):
    media = _film(tmp_path)

    updated = update_segment(media, 1, start_ms=1500, end_ms=2500)

    assert (updated.startMs, updated.endMs) == (1500, 2500)
    reloaded = load_timeline(media).segments[0]
    assert (reloaded.startMs, reloaded.endMs) == (1500, 2500)
    # A retime is not a decision, and must not invent one
    assert reloaded.approved is None
    assert reloaded.category == "nudity"
    assert reloaded.engine == "vlm"
    assert reloaded.recommendedAction == "skip"


def test_inverted_span_is_corrected_not_stored(tmp_path):
    """An end before its start would skip nothing, or everything."""
    media = _film(tmp_path)

    updated = update_segment(media, 1, start_ms=9000, end_ms=3000)

    assert (updated.startMs, updated.endMs) == (3000, 9000)


def test_retime_and_decide_together(tmp_path):
    media = _film(tmp_path)

    update_segment(media, 2, start_ms=5200, approved=True)

    reloaded = load_timeline(media).segments[1]
    assert reloaded.startMs == 5200
    assert reloaded.endMs == 5800
    assert reloaded.approved is True


def test_delete_removes_only_that_finding_and_keeps_ids(tmp_path):
    media = _film(tmp_path)

    assert delete_segment(media, 1)

    survivors = load_timeline(media).segments
    assert [s.id for s in survivors] == [2]
    # Renumbering would change the id of a finding the reviewer is looking at
    assert survivors[0].startMs == 5000


def test_delete_unknown_segment_reports_failure(tmp_path):
    media = _film(tmp_path)
    assert not delete_segment(media, 99)
    assert len(load_timeline(media).segments) == 2


def test_created_finding_takes_an_id_above_the_high_water_mark(tmp_path):
    media = _film(tmp_path)

    created = create_segment(media, 30000, 31000, "profanity", "mute")

    assert created.id == 3
    assert created.engine == MANUAL_ENGINE
    # Adding a finding deliberately is itself the decision
    assert created.approved is True

    reloaded = load_timeline(media)
    assert len(reloaded.segments) == 3
    assert [s.startMs for s in reloaded.segments] == [1000, 5000, 30000]


def test_created_ids_are_never_reused(tmp_path):
    """A deleted id must not come back attached to a different finding."""
    media = _film(tmp_path)

    delete_segment(media, 2)
    first = create_segment(media, 30000, 31000, "profanity", "mute")
    delete_segment(media, first.id)
    second = create_segment(media, 40000, 41000, "violence", "skip")

    assert second.id > first.id


def test_can_add_a_finding_to_an_unanalyzed_film(tmp_path):
    media = tmp_path / "Unanalyzed.mkv"
    media.write_bytes(b"some bytes to fingerprint")

    created = create_segment(media, 1000, 2000, "profanity", "mute")

    assert created is not None and created.id == 1
    assert load_timeline(media).segments[0].engine == MANUAL_ENGINE


# -- re-analysis merge ------------------------------------------------------


def test_reanalysis_keeps_hand_made_findings(tmp_path):
    """Story 34: a model re-run must not discard an administrator's work."""
    prior = [
        Segment(id=1, startMs=1000, endMs=2000, category="nudity",
                confidence=0.9, engine="vlm", recommendedAction="skip"),
        Segment(id=2, startMs=8000, endMs=9000, category="profanity",
                confidence=1.0, engine=MANUAL_ENGINE, recommendedAction="mute",
                approved=True),
    ]
    fresh = [
        Segment(id=1, startMs=1100, endMs=2100, category="nudity",
                confidence=0.8, engine="vlm", recommendedAction="skip"),
    ]

    merged = merge_segments(prior, fresh, {"vlm"})

    manual = [s for s in merged if s.engine == MANUAL_ENGINE]
    assert len(manual) == 1
    assert manual[0].approved is True
    assert manual[0].startMs == 8000
    # the re-run engine's own prior finding was replaced, not duplicated
    assert len([s for s in merged if s.engine == "vlm"]) == 1
    assert [s.startMs for s in merged] == [1100, 8000]


def test_reanalysis_keeps_engines_that_did_not_run(tmp_path):
    """Re-running the visual pass must not erase profanity findings."""
    prior = [
        Segment(id=1, startMs=1000, endMs=2000, category="profanity",
                confidence=1.0, engine="subtitles", recommendedAction="mute",
                approved=True),
        Segment(id=2, startMs=4000, endMs=5000, category="nudity",
                confidence=0.7, engine="vlm", recommendedAction="skip"),
    ]
    fresh = [
        Segment(id=1, startMs=4200, endMs=5200, category="nudity",
                confidence=0.9, engine="vlm", recommendedAction="skip"),
    ]

    merged = merge_segments(prior, fresh, {"vlm"})

    subtitles = [s for s in merged if s.engine == "subtitles"]
    assert len(subtitles) == 1 and subtitles[0].approved is True


def test_decisions_carry_over_onto_the_same_finding(tmp_path):
    """Re-analysis should not send a reviewer back through settled work."""
    prior = [
        Segment(id=1, startMs=1000, endMs=2000, category="nudity",
                confidence=0.9, engine="vlm", recommendedAction="skip",
                engineRef="shot-4", approved=False),
    ]
    fresh = [
        Segment(id=1, startMs=1000, endMs=2000, category="nudity",
                confidence=0.95, engine="vlm", recommendedAction="skip",
                engineRef="shot-4"),
        Segment(id=2, startMs=7000, endMs=7500, category="nudity",
                confidence=0.6, engine="vlm", recommendedAction="skip",
                engineRef="shot-19"),
    ]

    merged = merge_segments(prior, fresh, {"vlm"})

    by_ref = {s.engineRef: s for s in merged}
    assert by_ref["shot-4"].approved is False  # rejection survived
    assert by_ref["shot-19"].approved is None  # genuinely new, still to review


def test_reanalysis_through_the_job_store_keeps_manual_work(tmp_path):
    """The API path writes the sidecar too, and must merge like batch does."""
    from worker.models import Job
    from worker.store import Store

    media = _film(tmp_path, [
        Segment(id=1, startMs=8000, endMs=9000, category="profanity", confidence=1.0,
                engine=MANUAL_ENGINE, recommendedAction="mute", approved=True),
    ])
    store = Store(db_path=tmp_path / "jobs.db")
    job = Job(id="j1", mediaPath=str(media), engine="vlm", mediaFingerprint="fp")
    store.save_job(job)

    store.save_timeline("j1", Timeline(mediaFingerprint="fp", segments=[
        Segment(id=1, startMs=1000, endMs=2000, category="nudity", confidence=0.9,
                engine="vlm", recommendedAction="skip"),
    ]))

    segments = load_timeline(media).segments
    assert {s.engine for s in segments} == {MANUAL_ENGINE, "vlm"}
    assert [s for s in segments if s.engine == MANUAL_ENGINE][0].approved is True


# -- HTTP contract ----------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    return TestClient(app)


def test_patch_route_applies_only_what_was_sent(client, tmp_path):
    _film(tmp_path)

    body = client.patch(
        "/api/segments/1", params={"path": "Film.mkv"}, json={"startMs": 1500}
    ).json()

    segment = [s for s in body["segments"] if s["id"] == 1][0]
    assert segment["startMs"] == 1500
    assert segment["endMs"] == 2000       # untouched
    assert segment["approved"] is None    # not sent, so not decided


def test_patch_route_can_clear_a_decision(client, tmp_path):
    _film(tmp_path)
    client.patch("/api/segments/1", params={"path": "Film.mkv"}, json={"approved": True})

    body = client.patch(
        "/api/segments/1", params={"path": "Film.mkv"}, json={"approved": None}
    ).json()

    assert [s for s in body["segments"] if s["id"] == 1][0]["approved"] is None


def test_create_and_delete_routes(client, tmp_path):
    _film(tmp_path)

    created = client.post(
        "/api/segments",
        params={"path": "Film.mkv"},
        json={"startMs": 30000, "endMs": 31000, "category": "profanity",
              "recommendedAction": "mute"},
    )
    assert created.status_code == 201
    new_id = created.json()["id"]
    assert created.json()["engine"] == MANUAL_ENGINE

    remaining = client.delete(
        f"/api/segments/{new_id}", params={"path": "Film.mkv"}
    ).json()["segments"]
    assert new_id not in [s["id"] for s in remaining]


def test_routes_accept_a_foreign_mount_path(client, tmp_path):
    _film(tmp_path)

    response = client.patch(
        "/api/segments/1",
        params={"path": "/volume1/Media/Movies/Film.mkv"},
        json={"approved": True},
    )

    assert response.status_code == 200
    assert load_timeline(tmp_path / "Film.mkv").segments[0].approved is True


def test_deleting_an_unknown_segment_is_a_404(client, tmp_path):
    _film(tmp_path)
    assert client.delete("/api/segments/99", params={"path": "Film.mkv"}).status_code == 404


def test_merge_does_not_mutate_the_callers_segments(tmp_path):
    fresh = [
        Segment(id=1, startMs=1000, endMs=2000, category="nudity",
                confidence=0.9, engine="vlm", recommendedAction="skip"),
    ]
    prior = [
        Segment(id=7, startMs=500, endMs=600, category="profanity",
                confidence=1.0, engine=MANUAL_ENGINE, recommendedAction="mute"),
    ]

    merged = merge_segments(prior, fresh, {"vlm"})

    assert fresh[0].id == 1  # the job's own timeline is untouched
    assert max(s.id for s in merged) == 8
