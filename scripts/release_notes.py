#!/usr/bin/env python3
"""Print the CHANGELOG section for a release tag, after verifying it.

Usage:
    python3 scripts/release_notes.py v1.1.0 > release-notes.md

Exits 1 (message on stderr) when the tag is not ``vMAJOR.MINOR.PATCH``, does
not match ``core/version.py``, or CHANGELOG.md has no non-empty
``## [MAJOR.MINOR.PATCH]`` section. The release workflow runs this before
publishing anything (see docs/RELEASING.md).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def notes_for(tag: str) -> str:
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag or "")
    if not match:
        raise ValueError(f"tag must look like v1.2.3, got {tag!r}")
    version = match.group(1)

    sys.path.insert(0, str(ROOT))
    from core.version import VERSION

    if VERSION != version:
        raise ValueError(f"tag {tag} but core/version.py has VERSION = {VERSION!r}")

    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    heading = f"## [{version}]"
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    except StopIteration:
        raise ValueError(f"CHANGELOG.md has no '{heading}' section") from None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
    section = "\n".join(lines[start + 1 : end]).strip()
    if not section:
        raise ValueError(f"CHANGELOG.md section '{heading}' is empty")
    return section + "\n"


def main(argv: list[str]) -> int:
    try:
        sys.stdout.write(notes_for(argv[0] if argv else ""))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
