#!/usr/bin/env python3
from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path

BLOCKED_GLOBS = (
    ".local/**",
    "logs/**",
    "node_modules/**",
    "admin-web/node_modules/**",
    "admin-web/.vite/**",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "data/admin_p0.db",
    "data/*/audit/**",
    "data/*/mapping/**",
    "data/*/slices/**",
    "data/*/exports/**",
)


def _staged_files() -> list[tuple[str, str]]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "-z"],
        check=True,
        capture_output=True,
    )
    raw_items = [
        item for item in proc.stdout.decode("utf-8", errors="ignore").split("\0") if item
    ]
    pairs: list[tuple[str, str]] = []
    for i in range(0, len(raw_items), 2):
        status = raw_items[i]
        path = raw_items[i + 1]
        pairs.append((status, path))
    return pairs


def main() -> int:
    blocked: list[str] = []
    for status, relpath in _staged_files():
        # Allow cleanup commits that only remove tracked runtime/cache artifacts.
        if status == "D":
            continue
        normalized = relpath.replace("\\", "/")
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in BLOCKED_GLOBS):
            blocked.append(normalized)
    if not blocked:
        return 0

    sys.stderr.write("pre-commit guard blocked runtime/cache artifacts:\n")
    for path in blocked:
        sys.stderr.write(f"  - {path}\n")
    sys.stderr.write(
        "\nMove runtime outputs under .local/runtime or unstage these files before committing.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
