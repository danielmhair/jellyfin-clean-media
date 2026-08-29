"""GET/PUT /api/settings and GET /api/browse (worker/main.py).

TestClient pattern mirrors tests/test_health.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from worker import settings as worker_settings
from worker.main import app

client = TestClient(app)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_settings, "_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(worker_settings, "_current", None)


def test_get_settings_returns_defaults_and_guidance_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"]["mediaRoots"] == ""
    assert body["settings"]["vlmHosts"] == ""
    assert body["settings"]["flagUnderwear"] is True
    assert body["vlmGuidanceDefault"]["female_topless"]


def test_put_settings_persists_and_get_reflects_it(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    payload = {
        "mediaRoots": "/movies",
        "vlmHosts": "http://gpu-box:11434",
        "vlmGuidance": {"kissing": "custom definition"},
        "flagMaleShirtless": True,
        "flagUnderwear": True,
        "flagAnyKissing": False,
        "profanityIncludeMild": True,
        "profanityIncludeBlasphemy": False,
        "profanityExtraWords": "heck, darn",
        "supervisorEnabled": False,
    }
    put_resp = client.put("/api/settings", json=payload)
    assert put_resp.status_code == 200
    assert put_resp.json()["settings"]["mediaRoots"] == "/movies"

    get_resp = client.get("/api/settings")
    saved = get_resp.json()["settings"]
    assert saved["mediaRoots"] == "/movies"
    assert saved["vlmHosts"] == "http://gpu-box:11434"
    assert saved["vlmGuidance"] == {"kissing": "custom definition"}
    assert saved["flagMaleShirtless"] is True
    assert saved["profanityExtraWords"] == "heck, darn"
    assert saved["supervisorEnabled"] is False


def test_put_settings_rejects_malformed_body(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    resp = client.put("/api/settings", json={"flagUnderwear": "not-a-bool"})
    assert resp.status_code == 422


def test_browse_lists_subdirectories(tmp_path):
    child = tmp_path / "Movies"
    child.mkdir()
    (tmp_path / "not-a-dir.txt").touch()

    resp = client.get("/api/browse", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(tmp_path)
    names = [d["name"] for d in body["dirs"]]
    assert "Movies" in names
    assert "not-a-dir.txt" not in names


def test_browse_empty_path_returns_starting_points():
    resp = client.get("/api/browse", params={"path": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == ""
    assert isinstance(body["dirs"], list)


def test_browse_rejects_a_file_path(tmp_path):
    f = tmp_path / "not-a-dir.txt"
    f.touch()
    resp = client.get("/api/browse", params={"path": str(f)})
    assert resp.status_code == 404
