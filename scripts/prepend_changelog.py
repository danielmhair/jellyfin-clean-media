#!/usr/bin/env python3
"""Insert a release entry into CHANGELOG.md, newest first.

Usage: prepend_changelog.py <version> <YYYY-MM-DD>
The notes come from the CHANGELOG env var. Entries are inserted right after the
`<!-- releases -->` sentinel, so the header/intro stay on top.
"""

import os
import sys

SENTINEL = "<!-- releases -->"


def main() -> int:
    version, date = sys.argv[1], sys.argv[2]
    notes = (os.environ.get("CHANGELOG") or "").strip() or "See the repository for changes."
    path = "CHANGELOG.md"

    text = open(path, encoding="utf-8").read() if os.path.exists(path) else (
        "# Changelog\n\n" + SENTINEL + "\n"
    )
    if SENTINEL not in text:
        text = text.rstrip() + "\n\n" + SENTINEL + "\n"

    entry = f"\n## {version} — {date}\n\n{notes}\n"
    text = text.replace(SENTINEL, SENTINEL + entry, 1)
    open(path, "w", encoding="utf-8").write(text)
    print(f"CHANGELOG.md updated with {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
