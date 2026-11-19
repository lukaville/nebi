"""Module runner for `python -m nebi_ai`."""

from __future__ import annotations

import sys

from nebi_ai.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
