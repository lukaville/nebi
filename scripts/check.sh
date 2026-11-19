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
PKGS="nebi-lib nebi-cockpit nebi-cli nebi-voice nebi-patch"
if "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
    PKGS="$PKGS nebi-ai"
fi
for pkg in $PKGS; do
    echo "== $pkg"
    (cd "$ROOT/$pkg" && "$PY" -m pytest -q)
done
