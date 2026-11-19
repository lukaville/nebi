"""Dependency rules of the monorepo, checked on the source (every import statement, including lazy ones inside
functions and `if TYPE_CHECKING:` blocks):

    nebi        -> (nothing of ours)
    nebi_cockpit  -> nebi
    nebi_cli      -> nebi_cockpit, nebi
    nebi_voice    -> nebi (only in its extension adapter, lazily; the voice itself needs none of ours)

Extensions (e.g. nebi_voice) live in their own packages and are only ever loaded through nebi's extension loader:
no core package imports them.

Run from the monorepo root:  .venv/bin/python -m pytest tests
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "nebi": ROOT / "nebi-lib" / "src" / "nebi",
    "nebi_cockpit": ROOT / "nebi-cockpit" / "src" / "nebi_cockpit",
    "nebi_cli": ROOT / "nebi-cli" / "src" / "nebi_cli",
    "nebi_voice": ROOT / "nebi-voice" / "src" / "nebi_voice",
}
FORBIDDEN = {
    "nebi": {"nebi_cockpit", "nebi_cli", "nebi_voice"},
    "nebi_cockpit": {"nebi_cli", "nebi_voice"},
    "nebi_cli": {"nebi_voice"},
    "nebi_voice": {"nebi_cockpit", "nebi_cli"},
}
WEB_STACK = {"fastapi", "starlette", "uvicorn"}


def imported_modules(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.lineno, node.module))
    return out


def violations(pkg: str, banned: set[str], *, skip_dirs: tuple[str, ...] = ()) -> list[str]:
    bad = []
    for path in sorted(PACKAGES[pkg].rglob("*.py")):
        rel = path.relative_to(PACKAGES[pkg])
        if rel.parts and rel.parts[0] in skip_dirs:
            continue
        for line, mod in imported_modules(path):
            if mod.split(".")[0] in banned:
                bad.append(f"{pkg}/{rel}:{line} imports {mod}")
    return bad


@pytest.mark.parametrize("pkg", sorted(PACKAGES))
def test_dependency_direction(pkg: str) -> None:
    assert PACKAGES[pkg].is_dir()
    assert violations(pkg, FORBIDDEN[pkg]) == []


def test_nebi_lib_has_no_web_server_outside_the_simulator() -> None:
    """The library has no web server / CLI; only `nebi.sim` (the mock robot's HTTP daemons, extra `nebi-lib[sim]`)
    may use FastAPI/uvicorn."""
    assert violations("nebi", WEB_STACK, skip_dirs=("sim", "proto")) == []
    assert not (PACKAGES["nebi"] / "cli.py").exists()
    assert not (PACKAGES["nebi"] / "server").exists() and not (PACKAGES["nebi"] / "agent").exists()


def test_nebi_cockpit_imports_without_nebi_cli() -> None:
    code = (
        "import sys; sys.modules['nebi_cli'] = None\n"
        "import os; os.environ['PYNEBI_DISABLE_EXTENSIONS'] = '1'\n"
        "from nebi_cockpit.app import create_app\n"
        "app = create_app(start_simulator=False)\n"
        "from fastapi.testclient import TestClient\n"
        "tc = TestClient(app)\n"
        "assert tc.get('/agent/ping').status_code == 404 and tc.get('/api/extensions').json() == {'extensions': []}\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=str(ROOT))


def test_nebi_voice_imports_without_nebi_lib() -> None:
    """The voice is usable standalone: only `nebi_voice.extension` touches nebi, and only inside functions."""
    code = "import sys; sys.modules['nebi'] = None\nimport nebi_voice, nebi_voice.extension, nebi_voice.cli\n"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=str(ROOT))
