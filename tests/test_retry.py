import subprocess

import av.error
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


def test_backoffs_param_overrides_the_default_attempt_count():
    """A long, drop-prone read (full-film audio decode) asks for more attempts."""
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 6:
            raise OSError("drop")
        return "ok"

    # Default schedule is 4 attempts and would give up; six zero-delay backoffs
    # give seven attempts, enough to reach the success on the sixth.
    assert (
        retry_media_read(flaky, transient=(OSError,), backoffs=(0.0,) * 6) == "ok"
    )
    assert attempts["n"] == 6


def test_pyav_dropped_read_is_retried_only_when_ffmpegerror_is_transient():
    """The original bug: a dropped SMB decode raises av.error.ArgumentError,

    which is a *ValueError* — not an OSError — so the old (OSError, RuntimeError)
    filter let it fail with zero retries. It must retry when FFmpegError is in the
    transient set, and must still not retry when it is not.
    """
    def drop():
        # exactly what PyAV raises for EINVAL 22 on a dropped share read
        raise av.error.ArgumentError(22, "Invalid argument", "\\\\Nas\\film.mkv")

    assert not issubclass(av.error.ArgumentError, (OSError, RuntimeError))

    old_filter = {"n": 0}

    def drop_counting():
        old_filter["n"] += 1
        drop()

    # Old filter: not caught -> surfaces immediately, no retry (the reported bug).
    with pytest.raises(av.error.ArgumentError):
        retry_media_read(drop_counting, transient=(OSError, RuntimeError))
    assert old_filter["n"] == 1

    # Fixed filter: caught and retried across the whole schedule before surfacing.
    new_filter = {"n": 0}

    def drop_counting_new():
        new_filter["n"] += 1
        drop()

    with pytest.raises(av.error.ArgumentError):
        retry_media_read(
            drop_counting_new,
            transient=(OSError, RuntimeError, av.error.FFmpegError),
            backoffs=(0.0, 0.0),
        )
    assert new_filter["n"] == 3  # first try + two retries
