"""Core tests are hermetic: installed nebi extensions (entry points, PYNEBI_EXTENSIONS) are never loaded.

Tests that exercise the extension API install their own test extensions with the `extensions` fixture.
"""

from __future__ import annotations

import os

os.environ["PYNEBI_DISABLE_EXTENSIONS"] = "1"
os.environ.pop("PYNEBI_EXTENSIONS", None)
os.environ["PYNEBI_TTS_SYSTEM"] = "0"  # never run the OS speech engine implicitly (tests opt in)

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from nebi.extension_loader import LoadedExtension, reset_extensions, use_extensions


@pytest.fixture()
def extensions() -> Iterator[Callable[..., list[LoadedExtension]]]:
    """`extensions(obj, ...)` makes these the process's extensions; everything is reset afterwards."""

    def install(*objs: Any) -> list[LoadedExtension]:
        reset_extensions()
        return use_extensions(objs)

    yield install
    reset_extensions()
