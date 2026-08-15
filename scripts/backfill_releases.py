#!/usr/bin/env python3
"""One-off: create a GitHub Release for every version in manifest.json.

Idempotent — skips any version that already has a release. For each one it tags
the commit that first added that version's zip (so the tags are historically
accurate), attaches the committed zip, and uses the manifest changelog as the
notes. Newest version is marked "Latest".

Run from the backfill-releases workflow, which provides gh + GH_TOKEN. Needs a
full checkout (fetch-depth: 0) so the historical commits are present.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile


def sh(args: list[str]) -> str:
    return subprocess.run(args, text=True, capture_output=True).stdout.strip()


def main() -> int:
    versions = json.load(open("manifest.json", encoding="utf-8"))[0]["versions"]
    newest = versions[0]["version"]
    existing = set(
        sh(["gh", "release", "list", "--limit", "500", "--json", "tagName",
            "--jq", ".[].tagName"]).split()
    )

    created = skipped = failed = 0
    # Oldest first, so the releases' creation order matches the version order.
    for v in reversed(versions):
        ver = v["version"]
        tag = f"v{ver}"
        if tag in existing:
            print(f"skip {tag} (already a release)")
            skipped += 1
            continue

        date = v.get("timestamp", "").split("T")[0]
        notes = (v.get("changelog") or "See the repository for changes.").strip()
        if date:
            notes = f"_Originally released {date}._\n\n{notes}"

        zip_path = f"plugin/releases/clean-media-{ver}.zip"
        target = sh(["git", "log", "--diff-filter=A", "-n", "1", "--format=%H", "--", zip_path])

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(notes)
            notes_file = f.name

        args = [
            "gh", "release", "create", tag,
            "--title", f"Clean Media {ver}",
            "--notes-file", notes_file,
            "--latest=true" if ver == newest else "--latest=false",
        ]
        if target:
            args += ["--target", target]
        if os.path.isfile(zip_path):
            args.append(zip_path)

        rc = subprocess.run(args).returncode
        if rc == 0:
            print(f"created {tag}")
            created += 1
        else:
            print(f"FAILED {tag}")
            failed += 1

    print(f"\ndone: {created} created, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
