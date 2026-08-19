"""The version is written once.

`pyproject.toml` carried a literal and `calsync/__init__.py` carried another.
They disagreed for the whole of 0.2 — the package said 0.1.0 — because nothing
imports `__version__` and nothing compared them.
"""

from __future__ import annotations

import importlib.metadata

import pytest

import calsync


def test_the_installed_version_is_the_one_in_the_package():
    """Skipped when running off `pythonpath` with no install, which pytest
    supports — there is no metadata to disagree with then."""
    try:
        installed = importlib.metadata.version("calsync")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("not installed; nothing to compare against")
    assert installed == calsync.__version__


def test_pyproject_declares_no_version_of_its_own():
    """The literal is what drifted. If one comes back, so does the drift."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():  # installed without the source tree
        pytest.skip("no pyproject.toml beside the tests")
    lines = [
        line for line in pyproject.read_text().splitlines()
        if line.strip().startswith("version =") and "attr" not in line
    ]
    assert not lines, f"version is declared twice: {lines}"
