"""Module runner for `python -m nebi_patch`."""

from __future__ import annotations

import sys

from nebi_patch.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
