#!/usr/bin/env bash
# Create nebi/.venv (if missing) and install the monorepo packages editable with their dev extras.
# NEBI_VENV: use another venv path (CI, other Python versions); PYTHON: interpreter that creates it.
# Extra editable packages (e.g. extensions kept in other repos): NEBI_EXTRA_PACKAGES="path/one path/two".
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
VENV="${NEBI_VENV:-.venv}"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -e "nebi-lib[dev]" -e "nebi-cockpit[dev]" -e "nebi-cli[dev]" -e "nebi-voice[dev]" -e "nebi-patch[dev]"
if "$VENV/bin/python" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
    "$VENV/bin/pip" install -e "nebi-ai[dev]"
fi
for ext in ${NEBI_EXTRA_PACKAGES:-}; do "$VENV/bin/pip" install -q -e "$ext"; done
