#!/usr/bin/env python3
"""Write a one-line release changelog into $GITHUB_ENV as CHANGELOG.

Asks Claude to summarise the change context (a merged PR and/or the commits
since the last release, passed in via the CONTEXT env var) into a concise,
user-facing sentence. Falls back to the commit subject (MSG) if
ANTHROPIC_API_KEY is missing or the API call fails — so the release pipeline
never breaks on this. Uses only the Python standard library (no pip install).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

MODEL = "claude-haiku-4-5-20251001"  # cheap; a changelog is a small summarisation


def emit(text: str) -> None:
    """Publish CHANGELOG to the job env (multiline-safe) and echo it."""
    text = " ".join(text.split()).strip() or "See the repository for changes."
    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a", encoding="utf-8") as f:
            f.write(f"CHANGELOG<<CL_EOF\n{text}\nCL_EOF\n")
    print(text)


def main() -> int:
    context = os.environ.get("CONTEXT", "").strip()
    first_msg = next((ln.strip() for ln in os.environ.get("MSG", "").splitlines() if ln.strip()), "")
    first_ctx = next((ln.strip() for ln in context.splitlines() if ln.strip()), "")
    fallback = first_msg or first_ctx or "See the repository for changes."

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or not context:
        emit(fallback)
        return 0

    prompt = (
        "You are writing the changelog for a new release of 'Clean Media', a "
        "self-hosted Jellyfin plugin that detects and skips objectionable content "
        "with an administrator review step. From the change context below, write "
        "ONE or TWO concise, user-facing sentences describing what changed for "
        "users (not internal/testing detail). No preamble, no 'Changelog:', no "
        "markdown — just the sentence(s).\n\n"
        + context[:6000]
    )
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        emit(text or fallback)
    except Exception as exc:  # noqa: BLE001 — any failure must fall back, not break the release
        print(f"changelog: Claude call failed ({exc}); using fallback", file=sys.stderr)
        emit(fallback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
