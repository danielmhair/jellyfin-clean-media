import subprocess

import pytest

from worker import retry as retry_mod
from worker.retry import retry_media_read


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep the retries but drop the real waits so the suite stays fast."""
    monkeypatch.setattr(retry_mod, "BACKOFFS_S", (0.0, 0.0, 0.0))


def test_returns_on_first_success():
    calls = []
    assert retry_media_read(lambda: calls.append(1) or "ok", transient=(OSError,)) == "ok"
    assert len(calls) == 1


def test_recovers_after_transient_failures():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError(22, "Invalid argument")  # the EINVAL a dropped SMB read raises
        return "ok"

    assert retry_media_read(flaky, transient=(OSError,)) == "ok"
    assert attempts["n"] == 3


def test_reraises_the_last_error_when_all_attempts_fail():
    def always_fails():
        raise subprocess.CalledProcessError(1, "ffprobe")

    with pytest.raises(subprocess.CalledProcessError):
        retry_media_read(always_fails, transient=(subprocess.CalledProcessError,))


def test_a_non_transient_error_is_not_retried():
    attempts = {"n": 0}

    def bad():
        attempts["n"] += 1
        raise ValueError("not in the transient set")

    with pytest.raises(ValueError):
        retry_media_read(bad, transient=(OSError,))
    assert attempts["n"] == 1


def test_on_retry_fires_before_each_retry_not_the_first_try():
    attempts = {"n": 0}
    seen = []

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("drop")
        return "ok"

    retry_media_read(
        flaky, transient=(OSError,), on_retry=lambda exc: seen.append(str(exc))
    )
    assert seen == ["drop", "drop"]  # fired before retry 2 and 3, not before try 1
