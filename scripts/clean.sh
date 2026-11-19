#!/usr/bin/env bash
# Delete build leftovers and caches under nebi/ (never touches any .venv). Run before publishing / creating the git repo.
# Afterwards re-register the editable installs so the venv keeps working (their *.egg-info is regenerated, gitignored):
#   .venv/bin/pip install -q --no-deps -e nebi-lib -e nebi-cockpit -e nebi-cli -e nebi-voice
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
find . -name .venv -prune -o -type d \( -name '*.egg-info' -o -name __pycache__ -o -name .pytest_cache \
    -o -name .mypy_cache -o -name .ruff_cache \) -prune -print -exec rm -rf {} +
find . -name .venv -prune -o \( -type f -name .DS_Store -print -delete \)
for d in dist build nebi-lib/dist nebi-lib/build nebi-cockpit/dist nebi-cockpit/build nebi-cli/dist nebi-cli/build \
         nebi-voice/dist nebi-voice/build; do
    if [ -d "$d" ]; then echo "./$d"; rm -rf "$d"; fi
done
