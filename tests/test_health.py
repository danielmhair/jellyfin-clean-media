"""The /api/health handshake the plugin reads to pair itself with the worker.

The plugin compares the reported ``apiVersion`` against the contract it was built
for and shows a compatible / update-needed banner, so these two fields are a
contract in their own right — pin their presence and type here.
"""

from fastapi.testclient import TestClient

from worker import API_VERSION, __version__
from worker.main import app

client = TestClient(app)


def test_health_reports_version_and_api_version():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()

    assert body["status"] == "ok"
    # The marketing/lockstep version the plugin shows ("Connected · v…").
    assert body["version"] == __version__
    # The HTTP-contract version the plugin gates compatibility on — an int, so
    # an old worker that omits it reads as 0 (too old) on the plugin side.
    assert body["apiVersion"] == API_VERSION
    assert isinstance(body["apiVersion"], int)


def test_api_version_is_a_positive_int():
    # A released contract is at least 1; 0 is reserved for pre-handshake workers.
    assert isinstance(API_VERSION, int)
    assert API_VERSION >= 1
