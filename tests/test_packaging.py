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


# --- the deployment assets --------------------------------------------------


def _deploy(name: str):
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "deploy" / name
    if not path.exists():  # installed without the source tree
        pytest.skip("no deploy/ beside the tests")
    return path.read_text()


def test_anonymous_read_is_granted_to_the_empty_user_and_nobody_else():
    """`user:` looks like the way to name an anonymous request and is not.

    Radicale's `from_file` backend skips any rule whose user pattern is empty,
    so the obvious spelling grants nothing at all and reports nothing about it.
    `.*` is the other tempting answer and is worse: it matches the *writer* too,
    and the first matching section wins, so the writer would be handed read-only
    permissions and every sync would start failing.
    """
    rights = _deploy("radicale/rights.anonymous")
    users = [
        line.split(":", 1)[1].strip()
        for line in rights.splitlines()
        if line.startswith("user:")
    ]
    assert users, "no rules in the file"
    assert set(users) == {"^$"}, f"anonymous rules must match only the empty user: {users}"


def test_anonymous_read_grants_no_write():
    """Read letters only. The point of the flag is a password-free *read*."""
    rights = _deploy("radicale/rights.anonymous")
    granted = {
        letter
        for line in rights.splitlines() if line.startswith("permissions:")
        for letter in line.split(":", 1)[1].strip()
    }
    assert granted <= {"R", "r"}, f"anonymous rules grant more than read: {granted}"


def test_the_anonymous_rules_are_appended_never_pasted_in():
    """They must come after the calsync and calreader rules, so the base file
    cannot contain them: the container concatenates, and the first matching
    section is the one that answers."""
    base = _deploy("radicale/rights")
    assert "^$" not in base, "the anonymous rules belong in rights.anonymous"
    assert "user: .*" not in base
