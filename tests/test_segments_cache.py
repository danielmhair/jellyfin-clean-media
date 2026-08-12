"""The /api/segments negative-lookup cache.

The Jellyfin plugin polls /api/segments for every item a client plays. For an
unanalyzed film there is no timeline, and reaching that verdict fingerprints the
media — a 24 MB read off the (often NAS) share, per poll. A client playing an
unanalyzed film hits it ~1/sec, so the miss is cached briefly to spare the NAS.
These tests pin that behaviour, and that a freshly analyzed film is not stuck
serving a stale 404.
"""

import json

import pytest
from fastapi.testclient import TestClient

import worker.main as main
from worker.main import app
from worker.models import Segment, Timeline
from worker.review import sidecar_for


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    main._segments_neg_cache.clear()  # isolate from other tests / prior polls
    return TestClient(app)


def _film(tmp_path, name):
    media = tmp_path / name
    media.write_bytes(b"pretend video bytes")
    return media


def _approved_sidecar(media):
    seg = Segment(id=1, startMs=0, endMs=500, category="profanity",
                  confidence=1.0, engine="subtitles", recommendedAction="skip",
                  approved=True)
    sidecar_for(media).write_text(
        json.dumps(Timeline(mediaFingerprint="fp", segments=[seg]).model_dump()),
        encoding="utf-8",
    )


def test_repeated_miss_is_served_from_cache_without_touching_timeline(client, tmp_path, monkeypatch):
    _film(tmp_path, "Unanalyzed.mkv")  # resolves, but no sidecar → 404

    calls = {"n": 0}
    real = main.timeline_for
    monkeypatch.setattr(main, "timeline_for",
                        lambda media: (calls.__setitem__("n", calls["n"] + 1), real(media))[1])

    r1 = client.get("/api/segments", params={"path": "Unanalyzed.mkv"})
    r2 = client.get("/api/segments", params={"path": "Unanalyzed.mkv"})
    r3 = client.get("/api/segments", params={"path": "Unanalyzed.mkv"})

    assert r1.status_code == r2.status_code == r3.status_code == 404
    # Only the first poll did the expensive lookup; the rest hit the cache.
    assert calls["n"] == 1


def test_unresolvable_path_is_also_cached(client, monkeypatch):
    # A path that maps to no local file 404s too, and should be remembered so a
    # client hammering a bad path doesn't re-walk the index each time.
    calls = {"n": 0}
    real = main.resolve_media
    monkeypatch.setattr(main, "resolve_media",
                        lambda p: (calls.__setitem__("n", calls["n"] + 1), real(p))[1])

    for _ in range(3):
        assert client.get("/api/segments", params={"path": "/nope/Ghost.mkv"}).status_code == 404
    assert calls["n"] == 1


def test_reindex_clears_the_cache_so_a_new_sidecar_is_served(client, tmp_path):
    media = _film(tmp_path, "JustFinished.mkv")

    # First poll: unanalyzed → 404, miss cached.
    assert client.get("/api/segments", params={"path": "JustFinished.mkv"}).status_code == 404
    assert "JustFinished.mkv" in main._segments_neg_cache

    # Analysis writes a sidecar. Within the TTL the cached miss would still 404…
    _approved_sidecar(media)
    assert client.get("/api/segments", params={"path": "JustFinished.mkv"}).status_code == 404

    # …but a reindex (what the batch pings on completion) clears it immediately.
    client.post("/api/reindex")
    assert main._segments_neg_cache == {}
    r = client.get("/api/segments", params={"path": "JustFinished.mkv"})
    assert r.status_code == 200
    assert len(r.json()["segments"]) == 1


def test_expired_entry_is_rechecked(client, tmp_path, monkeypatch):
    media = _film(tmp_path, "Expiring.mkv")
    assert client.get("/api/segments", params={"path": "Expiring.mkv"}).status_code == 404

    # Sidecar appears, and the cache entry ages past its TTL (simulate by making
    # the stored timestamp old) — the next poll must re-check, not serve a stale 404.
    _approved_sidecar(media)
    monkeypatch.setattr(main, "_SEGMENTS_NEG_TTL_S", 0.0)
    r = client.get("/api/segments", params={"path": "Expiring.mkv"})
    assert r.status_code == 200
