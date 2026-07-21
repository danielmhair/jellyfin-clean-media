"""Re-apply local patches to installed packages after `uv sync`.

pureframe 0.1.0b7 ships bugs that make it unusable (ffmpeg stderr-pipe
deadlock, no seeking so every shot decodes the whole movie, numpy types
crashing plan serialization). Until fixes land upstream, the patched files
live in patches/pureframe/ and this script copies them into site-packages.

Usage: uv run python patches/apply_patches.py
"""

from __future__ import annotations

import shutil
import sysconfig
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = Path(sysconfig.get_paths()["purelib"])

PATCHES = {
    HERE / "pureframe" / "sample.py": SITE / "pureframe" / "pipeline" / "sample.py",
    HERE / "pureframe" / "plan.py": SITE / "pureframe" / "pipeline" / "render" / "plan.py",
}


def main() -> None:
    for src, dst in PATCHES.items():
        if not dst.parent.is_dir():
            print(f"SKIP {dst} (package not installed)")
            continue
        already = dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
        if already:
            print(f"OK   {dst} (already patched)")
            continue
        shutil.copyfile(src, dst)
        # invalidate stale bytecode
        pycache = dst.parent / "__pycache__"
        if pycache.is_dir():
            for pyc in pycache.glob(dst.stem + ".*.pyc"):
                pyc.unlink(missing_ok=True)
        print(f"PATCH {dst}")


if __name__ == "__main__":
    main()
