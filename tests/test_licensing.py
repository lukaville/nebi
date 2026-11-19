"""Licence and metadata consistency of the monorepo packages.

Each package ships copies of the root LICENSE / NOTICE (PEP 639 `license-files` cannot point outside the package
directory); they must stay identical to the root files. Versions come from each pyproject.toml only.

Run from the monorepo root:  .venv/bin/python -m pytest tests
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the tomli backport (installed with mypy/pytest)
    tomllib = pytest.importorskip("tomli")

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("nebi-lib", "nebi-cockpit", "nebi-cli", "nebi-voice", "nebi-patch", "nebi-ai")


def _project(pkg: str) -> dict:
    return tomllib.loads((ROOT / pkg / "pyproject.toml").read_text())["project"]


@pytest.mark.parametrize("pkg", PACKAGES)
@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_license_copies_match_root(pkg: str, name: str) -> None:
    assert (ROOT / pkg / name).read_bytes() == (ROOT / name).read_bytes(), (
        f"{pkg}/{name} differs from the root {name}: for p in nebi nebi-cockpit nebi-cli nebi-voice; do cp LICENSE NOTICE $p/; done"
    )


@pytest.mark.parametrize("pkg", PACKAGES)
def test_pyproject_license_metadata(pkg: str) -> None:
    project = _project(pkg)
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "NOTICE"]
    assert not [c for c in project.get("classifiers", []) if c.startswith("License ::")]  # PEP 639: expression only


def test_runtime_versions_come_from_metadata() -> None:
    import nebi
    import nebi_cli
    import nebi_patch
    import nebi_voice
    from nebi_cockpit.app import APP_VERSION

    assert nebi.__version__ == _project("nebi-lib")["version"]
    assert nebi_cli.__version__ == _project("nebi-cli")["version"]
    assert nebi_voice.__version__ == _project("nebi-voice")["version"]
    assert nebi_patch.__version__ == _project("nebi-patch")["version"]
    assert _project("nebi-cockpit")["version"] == APP_VERSION
    if sys.version_info >= (3, 11):
        import nebi_ai

        assert nebi_ai.__version__ == _project("nebi-ai")["version"]
