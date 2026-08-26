"""Stage a network-share media file onto local disk before a long read.

The user's media lives on a flaky SMB share (see ``worker/retry.py``). Small
reads succeed — ffprobe and PyAV's ``av.open`` only touch the header — but a
*full-file* decode runs for minutes, and the share reliably drops at least one
read before it finishes. A streaming decode cannot resume: faster-whisper (and
ffmpeg) restart from zero on a dropped read, so on a large file they may never
get a clean pass. Measured on this share, a run of consecutive full reads of a
7.7 GB film *all* dropped, at a different point each time (7, 23, 74 min in).

``robocopy``'s restartable mode (``/Z``) *resumes* a partially-copied file across
drops instead of restarting, so it grinds a large file to completion where a
streaming read cannot — verified: the same 7.7 GB film that never decoded over
the share copied byte-exact in ~14 min, then decoded locally in 13 s. So copy
once to local disk, decode there (where nothing drops), and delete.

Only large UNC paths on Windows are staged. A local path, or a small file the
retry path (``worker/retry.py``) already handles by re-reading, is used in place
so tests and local media pay nothing.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Optional

# Below this, a full read is short enough that an occasional dropped read is
# cheaply recovered by re-reading (retry_media_read); above it, a streaming read
# runs long enough that a drop is near-certain and a from-scratch retry may never
# converge, so the resumable copy earns its cost. Drops were seen as early as ~1
# minute into a read, so this is deliberately conservative.
STAGE_MIN_BYTES = 1 * 1024**3  # 1 GiB


def _is_unc(path: Path) -> bool:
    # UNC: \\server\share\...  — the flaky SMB share the worker reads from.
    return os.name == "nt" and str(path).startswith("\\\\")


def should_stage(path: Path) -> bool:
    """True when ``path`` is a large file on the network share worth staging.

    robocopy is a Windows built-in, so this is true wherever a UNC path is (the
    ``os.name == "nt"`` guard in ``_is_unc`` already implies it). The explicit
    ``which`` check is belt-and-suspenders: on the vanishingly unlikely box where
    robocopy is absent, skip staging and let the retry path try, rather than
    crashing on a missing tool. ``setup.sh`` reports robocopy so this is visible.
    """
    if not _is_unc(path) or shutil.which("robocopy") is None:
        return False
    try:
        return path.stat().st_size >= STAGE_MIN_BYTES
    except OSError:
        return False


@contextlib.contextmanager
def local_media(
    path: Path, progress: Optional[Callable[[float, str], None]] = None
) -> Iterator[Path]:
    """Yield a local path to ``path``'s bytes, staging a large network file first.

    A local path, or a small network file, is yielded unchanged. A large UNC file
    is copied to a temp dir with a resumable ``robocopy``, its size verified
    against the source, and the copy removed on exit. Staging failure surfaces —
    the original path is *not* silently substituted, since the caller's decode
    would only hit the same flaky share again.
    """
    if not should_stage(path):
        yield path
        return

    expected = path.stat().st_size  # a stat is metadata-only: reliable on the share
    staging_dir = Path(tempfile.mkdtemp(prefix="cleanmedia-stage-"))
    try:
        if progress is not None:
            progress(0.0, f"staging {path.name} to local disk ({expected / 1024**3:.1f} GB)")
        dst = staging_dir / path.name
        _robocopy(path, dst)
        got = dst.stat().st_size
        if got != expected:
            raise OSError(
                f"staged copy of {path.name} is {got} bytes, source is {expected} "
                "— copy stopped short"
            )
        yield dst
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _robocopy(src: Path, dst: Path) -> None:
    """Copy one file with robocopy's restartable mode, raising on real failure.

    robocopy addresses a *file within a directory*, not path-to-path, and signals
    result through its exit code: 0-7 are success (bit 0 = files copied, bit 1 =
    extras, bit 2 = mismatches), 8 and above mean a copy failed. Treating any
    non-zero as failure — the usual mistake — would reject a normal success.
    """
    proc = subprocess.run(
        [
            "robocopy", str(src.parent), str(dst.parent), src.name,
            "/Z",       # restartable mode: resume a partial file across dropped reads
            "/R:100",   # retry a failed read up to 100 times ...
            "/W:2",     # ... waiting 2s between tries (a re-established SMB session)
            "/NP", "/NDL", "/NJH", "/NJS", "/NC", "/NS",  # quiet: no per-file noise
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode >= 8:
        raise OSError(
            f"robocopy failed (exit {proc.returncode}) staging {src.name}:\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
