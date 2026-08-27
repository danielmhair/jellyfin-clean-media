"""worker/update.py: the GitHub release check and the apply-on-click flow.

Network calls are isolated behind update._fetch_json / update._download, and
the file swap behind update._copy_into_repo, so these are exercised directly
rather than by mocking urllib internals or touching the real checkout.
"""

import threading
import time

import pytest

from worker import __version__, update


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts from a clean cache, whatever earlier tests left."""
    with update._lock:
        update._state.update(
            {
                "latestVersion": None,
                "releaseUrl": None,
                "notes": None,
                "checkedAt": None,
                "checkError": None,
                "updating": False,
                "applyError": None,
                "appliedAt": None,
            }
        )
    yield


# ---- version comparison -----------------------------------------------------


def test_version_tuple_parses_and_ignores_leading_v():
    assert update._version_tuple("v1.2.3") == (1, 2, 3)
    assert update._version_tuple("1.2.3") == (1, 2, 3)


def test_version_tuple_pads_short_versions():
    assert update._version_tuple("1.2") == (1, 2, 0)
    assert update._version_tuple("1") == (1, 0, 0)


def test_is_newer():
    assert update._is_newer("0.2.17", "0.2.16") is True
    assert update._is_newer("0.2.16", "0.2.16") is False
    assert update._is_newer("0.2.15", "0.2.16") is False
    assert update._is_newer("1.0.0", "0.2.16") is True


# ---- status() / check_now() -------------------------------------------------


def test_status_before_any_check_reports_no_update():
    s = update.status()
    assert s["currentVersion"] == __version__
    assert s["latestVersion"] is None
    assert s["updateAvailable"] is False


def test_check_now_updates_cache_and_flags_update_available(monkeypatch):
    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {
            "tag_name": "v99.0.0",
            "html_url": "https://example.invalid/releases/v99.0.0",
            "body": "release notes",
        },
    )
    s = update.check_now()
    assert s["latestVersion"] == "99.0.0"
    assert s["updateAvailable"] is True
    assert s["releaseUrl"] == "https://example.invalid/releases/v99.0.0"
    assert s["notes"] == "release notes"
    assert s["checkError"] is None


def test_check_now_when_already_current(monkeypatch):
    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {"tag_name": f"v{__version__}"},
    )
    s = update.check_now()
    assert s["updateAvailable"] is False


def test_check_now_survives_network_failure(monkeypatch):
    def boom(url, timeout=10.0):
        raise OSError("network unreachable")

    monkeypatch.setattr(update, "_fetch_json", boom)
    s = update.check_now()
    assert s["updateAvailable"] is False
    assert "network unreachable" in s["checkError"]


# ---- begin_apply() -----------------------------------------------------------


def test_begin_apply_raises_when_nothing_newer(monkeypatch):
    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {"tag_name": f"v{__version__}"},
    )
    with pytest.raises(update.NoUpdateAvailable):
        update.begin_apply()
    assert update.status()["updating"] is False


def test_begin_apply_runs_apply_and_restart_in_background(monkeypatch):
    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {"tag_name": "v99.0.0"},
    )
    applied_with = []
    monkeypatch.setattr(update, "_apply", lambda version: applied_with.append(version))
    monkeypatch.setattr(update, "_restart_service", lambda: None)

    update.begin_apply()

    for _ in range(100):
        if not update.status()["updating"]:
            break
        time.sleep(0.02)

    assert applied_with == ["99.0.0"]
    s = update.status()
    assert s["updating"] is False
    assert s["applyError"] is None
    assert s["appliedAt"] is not None


def test_begin_apply_records_error_from_failed_apply(monkeypatch):
    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {"tag_name": "v99.0.0"},
    )

    def failing_apply(version):
        raise RuntimeError("disk full")

    monkeypatch.setattr(update, "_apply", failing_apply)
    monkeypatch.setattr(update, "_restart_service", lambda: None)

    update.begin_apply()

    for _ in range(100):
        if not update.status()["updating"]:
            break
        time.sleep(0.02)

    s = update.status()
    assert s["updating"] is False
    assert "disk full" in s["applyError"]
    assert s["appliedAt"] is None


def test_begin_apply_refuses_a_second_call_while_running(monkeypatch):
    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {"tag_name": "v99.0.0"},
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_apply(version):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(update, "_apply", blocking_apply)
    monkeypatch.setattr(update, "_restart_service", lambda: None)

    update.begin_apply()
    assert started.wait(timeout=5)

    with pytest.raises(update.UpdateInProgress):
        update.begin_apply()

    release.set()
    for _ in range(100):
        if not update.status()["updating"]:
            break
        time.sleep(0.02)


# ---- _copy_into_repo() -------------------------------------------------------


def test_copy_into_repo_overwrites_tracked_files_and_leaves_untracked_alone(tmp_path):
    extracted = tmp_path / "extracted-root"
    (extracted / "worker").mkdir(parents=True)
    (extracted / "worker" / "main.py").write_text("# new code")
    (extracted / "README.md").write_text("new readme")

    repo = tmp_path / "repo"
    (repo / "worker").mkdir(parents=True)
    (repo / "worker" / "main.py").write_text("# old code")
    (repo / "data").mkdir()
    (repo / "data" / "cleanmedia.db").write_text("precious jobs data")

    update._copy_into_repo(extracted, repo)

    assert (repo / "worker" / "main.py").read_text() == "# new code"
    assert (repo / "README.md").read_text() == "new readme"
    # Untracked (not present in the "release") paths are never touched.
    assert (repo / "data" / "cleanmedia.db").read_text() == "precious jobs data"


# ---- /api/health and /api/update/* wiring ------------------------------------


def test_health_reports_update_fields(monkeypatch):
    from fastapi.testclient import TestClient

    from worker.main import app

    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {"tag_name": "v99.0.0"},
    )
    update.check_now()

    client = TestClient(app)
    body = client.get("/api/health").json()
    assert body["updateAvailable"] is True
    assert body["latestVersion"] == "99.0.0"


def test_update_apply_endpoint_400_when_current(monkeypatch):
    from fastapi.testclient import TestClient

    from worker.main import app

    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {"tag_name": f"v{__version__}"},
    )

    client = TestClient(app)
    r = client.post("/api/update/apply")
    assert r.status_code == 400


def test_update_apply_endpoint_409_when_already_updating(monkeypatch):
    from fastapi.testclient import TestClient

    from worker.main import app

    monkeypatch.setattr(
        update,
        "_fetch_json",
        lambda url, timeout=10.0: {"tag_name": "v99.0.0"},
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_apply(version):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(update, "_apply", blocking_apply)
    monkeypatch.setattr(update, "_restart_service", lambda: None)

    client = TestClient(app)
    r1 = client.post("/api/update/apply")
    assert r1.status_code == 200
    assert started.wait(timeout=5)

    r2 = client.post("/api/update/apply")
    assert r2.status_code == 409

    release.set()
    for _ in range(100):
        if not update.status()["updating"]:
            break
        time.sleep(0.02)
