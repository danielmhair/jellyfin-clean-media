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
        gb = expected / 1024**3
        if progress is not None:
            progress(0.0, f"staging {path.name} to local disk ({gb:.1f} GB)")

        def on_percent(pct: float) -> None:
            if progress is not None:
                progress(
                    min(pct / 100, 0.99),
                    f"staging {path.name} to local disk ({pct:.0f}% of {gb:.1f} GB)",
                )

        dst = staging_dir / path.name
        _robocopy(path, dst, on_percent if progress is not None else None)
        got = dst.stat().st_size
        if got != expected:
            raise OSError(
                f"staged copy of {path.name} is {got} bytes, source is {expected} "
                "— copy stopped short"
            )
        yield dst
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


@contextlib.contextmanager
def local_output(
    path: Path, progress: Optional[Callable[[float, str], None]] = None
) -> Iterator[Path]:
    """Yield a local path to write to; copy it to ``path`` only on success.

    The write-side counterpart to :func:`local_media`. A render writes for
    hours straight, and the same flaky share that drops a long sequential
    *read* drops a long sequential *write* too — ffmpeg cannot resume a
    dropped write, only restart it, so a multi-hour render straight to the
    share may never finish cleanly. Rendering to local disk and moving the
    finished file with one resumable robocopy sidesteps that: nothing is ever
    written at ``path`` until the local render is complete, so a failed
    render never leaves a broken file on the share. A local ``path`` is used
    unchanged (tests, local media pay nothing).
    """
    if not _is_unc(path) or shutil.which("robocopy") is None:
        yield path
        return

    staging_dir = Path(tempfile.mkdtemp(prefix="cleanmedia-outstage-"))
    local_path = staging_dir / path.name
    try:
        yield local_path
        expected = local_path.stat().st_size
        gb = expected / 1024**3
        if progress is not None:
            progress(0.99, f"copying {path.name} to the network ({gb:.1f} GB)")

        def on_percent(pct: float) -> None:
            if progress is not None:
                # Pinned at 0.99, same as the message above: the numeric
                # fraction must not regress this late in an hours-long render
                # just because the final network copy has its own 0-100%.
                progress(
                    0.99,
                    f"copying {path.name} to the network ({pct:.0f}% of {gb:.1f} GB)",
                )

        path.parent.mkdir(parents=True, exist_ok=True)
        _robocopy(local_path, path, on_percent if progress is not None else None)
        got = path.stat().st_size
        if got != expected:
            raise OSError(
                f"network copy of {path.name} is {got} bytes, the local "
                f"render is {expected} — copy stopped short"
            )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _robocopy(
    src: Path, dst: Path, on_percent: Optional[Callable[[float], None]] = None
) -> None:
    """Copy one file with robocopy's restartable mode, raising on real failure.

    robocopy addresses a *file within a directory*, not path-to-path, and signals
    result through its exit code: 0-7 are success (bit 0 = files copied, bit 1 =
    extras, bit 2 = mismatches), 8 and above mean a copy failed. Treating any
    non-zero as failure — the usual mistake — would reject a normal success.

    Without ``on_percent``, robocopy's own progress output is suppressed
    (``/NP``) and the whole run is captured in one blocking call. With it, the
    same per-file percentage is kept instead, so a caller's progress bar moves
    during a multi-minute copy instead of sitting still until it finishes or
    fails.
    """
    args = [
        "robocopy", str(src.parent), str(dst.parent), src.name,
        "/Z",       # restartable mode: resume a partial file across dropped reads
        "/R:100",   # retry a failed read up to 100 times ...
        "/W:2",     # ... waiting 2s between tries (a re-established SMB session)
        "/NDL", "/NJH", "/NJS", "/NC", "/NS",  # quiet: no per-file noise
    ]
    if on_percent is None:
        proc = subprocess.run(args + ["/NP"], capture_output=True, text=True)
        if proc.returncode >= 8:
            raise OSError(
                f"robocopy failed (exit {proc.returncode}) staging {src.name}:\n"
                f"{proc.stdout}\n{proc.stderr}"
            )
        return

    # robocopy rewrites its percentage in place with a bare \r, not a \n, so
    # iterating stdout by line (which only splits on \n) never sees it — read
    # byte by byte and treat either as a line terminator.
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout is not None
    tail: list[str] = []
    buf = ""
    while True:
        ch = proc.stdout.read(1)
        if ch == "":
            break
        if ch not in "\r\n":
            buf += ch
            continue
        line = buf.strip()
        buf = ""
        if not line:
            continue
        tail.append(line)
        del tail[:-40]
        if line.endswith("%"):
            try:
                on_percent(float(line[:-1]))
            except ValueError:
                pass
    proc.wait()
    if proc.returncode >= 8:
        raise OSError(
            f"robocopy failed (exit {proc.returncode}) staging {src.name}:\n"
            + "\n".join(tail)
        )
