#!/bin/bash
# Bump version, commit, tag.
# Usage: ./release.sh [patch|minor|major]   (default: patch)
set -e
cd "$(dirname "$0")"
level="${1:-patch}"
bumpversion "$level"
echo "done — push with: git push origin main && git push --tags"
