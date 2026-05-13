#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: release.sh <version> (e.g. 0.2.1 or 0.2.1rc1)" >&2
  exit 1
fi

v="$1"
python scripts/bump_version.py "$v"
git diff --stat
printf "Proceed to commit and tag v%s? [y/N] " "$v"
read -r ok
if [ "$ok" != "y" ]; then
  echo "Aborted, files left modified."
  exit 1
fi

git add CHANGELOG.md pyproject.toml syll/__init__.py
git commit -m "release: v$v"
git tag "v$v"
echo "Now run: git push && git push --tags"
