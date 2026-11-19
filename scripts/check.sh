#!/usr/bin/env bash
# Lint, type-check and test the whole monorepo (run from anywhere).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${NEBI_VENV:-$ROOT/.venv}"             # NEBI_VENV: another venv (CI, other Python versions)
PY="$VENV/bin/python"
cd "$ROOT"
"$VENV/bin/ruff" check .
"$VENV/bin/mypy"
"$PY" -m pytest -q tests                     # architecture / dependency rules
for pkg in nebi-lib nebi-cockpit nebi-cli nebi-voice nebi-patch; do
    echo "== $pkg"
    (cd "$ROOT/$pkg" && "$PY" -m pytest -q)
done
