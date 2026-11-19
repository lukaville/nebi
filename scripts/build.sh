#!/usr/bin/env bash
# Build sdist + wheel of every package into dist/ and validate their metadata (used by CI and the publish workflow).
# Needs `build` and `twine` in the current Python: python -m pip install build twine
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
cd "$ROOT"
rm -rf dist
for pkg in nebi-lib nebi-cockpit nebi-cli nebi-voice nebi-patch; do
    echo "== $pkg"
    "$PY" -m build --outdir dist "$pkg"
    rm -rf "$pkg/build"
done
"$PY" -m twine check --strict dist/*
ls -l dist
