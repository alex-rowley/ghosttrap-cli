#!/bin/bash
# Freeze the previous release's skill hash, then bump version.
# Usage: ./release.sh [patch|minor|major]   (default: patch)
set -e
cd "$(dirname "$0")"
level="${1:-patch}"
python3 tools/freeze_skill_hash.py
if ! git diff --quiet ghosttrap_cli/cli.py; then
    git add ghosttrap_cli/cli.py
    git commit -m "{chore}: freeze prior skill hash"
fi
bumpversion "$level"
echo "done — push with: git push origin main && git push --tags"
