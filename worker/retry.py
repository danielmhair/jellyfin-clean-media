"""Bounded retry for reads off a flaky network share.

A NAS/SMB mount drops reads intermittently: ffprobe exits non-zero, an
ffmpeg or PySceneDetect decode stops short of the film, and faster-whisper's
PyAV layer raises ``OSError``/``FileNotFoundError`` (EINVAL 22, ENOENT 2) — all
on a file that is perfectly readable a moment later. This was verified against
the actual share: the same UNC path that failed ``av.open`` opened cleanly on
the very next attempt, and ffprobe that had exited 1 returned the duration.

So one dropped read must not fail a job that runs for minutes-to-hours. Retry a
few times with a short backoff; a genuinely unreadable or truncated file fails
every attempt and the final error is raised unchanged, so real problems still
surface (just a few seconds later) with their original message.

The visual pass already survives this class of failure for its Ollama
requests (``worker/engines/vlm_engine.py`` retries + fails over + checkpoints).
This is the same idea for the *local* media reads the other engines depend on.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

# Four attempts total: the first is immediate, then a 3s / 6s / 9s pause before
# each retry — long enough for an SMB session to re-establish, short against a
# multi-hour job. Tests set this to () to run a single attempt with no sleeps.
BACKOFFS_S: tuple[float, ...] = (3.0, 6.0, 9.0)


def retry_media_read(
    op: Callable[[], T],
    *,
    transient: tuple[type[BaseException], ...],
    on_retry: Optional[Callable[[BaseException], None]] = None,
) -> T:
    """Run ``op()``, retrying the listed transient exceptions with backoff.

    ``on_retry`` (if given) is called with the caught exception just before each
    pause — used to emit a progress line so a stalled read looks like a retry,
    not a hang. The final failure re-raises the last exception verbatim.
    """
    last: Optional[BaseException] = None
    for attempt, backoff in enumerate((0.0, *BACKOFFS_S)):
        if attempt > 0:  # a retry, not the first try
            if on_retry is not None and last is not None:
                on_retry(last)
            if backoff:
                time.sleep(backoff)
        try:
            return op()
        except transient as exc:
            last = exc
    assert last is not None  # the loop ran at least once
    raise last
