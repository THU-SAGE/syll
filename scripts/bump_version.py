#!/usr/bin/env python3
"""Prepare a Syll release by updating version files and the changelog."""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "syll" / "__init__.py"
EMPTY_UNRELEASED = """## [Unreleased]

### Added

### Changed

### Fixed

### Removed

### Security

"""
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?$")
UNRELEASED_RE = re.compile(r"## \[Unreleased\]\n(?P<body>.*?)(?=\n## \[)", re.DOTALL)


def run_git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def ensure_clean_main() -> None:
    branch = run_git("branch", "--show-current")
    if branch != "main":
        raise SystemExit(f"release must run on main, found {branch!r}")
    subprocess.check_call(["git", "diff", "--quiet"], cwd=ROOT)
    subprocess.check_call(["git", "diff", "--cached", "--quiet"], cwd=ROOT)


def ensure_unreleased_has_content(body: str) -> None:
    meaningful = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("###"):
            continue
        meaningful.append(stripped)
    if not meaningful:
        raise SystemExit("CHANGELOG.md [Unreleased] is empty")


def bump_changelog(version: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    match = UNRELEASED_RE.search(text)
    if not match:
        raise SystemExit("CHANGELOG.md is missing ## [Unreleased]")
    body = match.group("body")
    ensure_unreleased_has_content(body)
    release_heading = f"## [{version}] — {date.today().isoformat()}"
    replacement = EMPTY_UNRELEASED + release_heading + "\n" + body
    CHANGELOG.write_text(text[: match.start()] + replacement + text[match.end() :], encoding="utf-8")


def replace_once(path: Path, pattern: str, repl: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"expected exactly one version match in {path}")
    path.write_text(new_text, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: bump_version.py <version>  (e.g. 0.2.1 or 0.2.1rc1)", file=sys.stderr)
        return 2
    version = argv[1]
    if not VERSION_RE.match(version):
        raise SystemExit("version must be PEP 440-like: 0.2.1, 0.2.1rc1, 0.2.1a1, or 0.2.1b1")

    ensure_clean_main()
    bump_changelog(version)
    replace_once(PYPROJECT, r'^version = "[^"]+"$', f'version = "{version}"')
    replace_once(INIT, r'^__version__ = "[^"]+"$', f'__version__ = "{version}"')
    print(f"Prepared release v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
