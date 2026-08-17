"""Choosing where events go.

The registry advertised three kinds and the CLI could reach two, by way of a
hardcoded "ics_file when --out is given, otherwise CalDAV". Pluggability that
nothing can select is not pluggability, and the console had grown its own copy
of the CalDAV construction, which is how two callers start disagreeing about
where a family's events are written.
"""

from __future__ import annotations

import pytest

from calsync import db, targeting
from calsync.secrets import SecretStore
from calsync.targets import TargetError


@pytest.fixture
def conn(tmp_path):
    return db.open_db(tmp_path / "calsync.db")


@pytest.fixture
def secrets(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text('{"radicale_password": "hunter2"}')
    path.chmod(0o600)
    return SecretStore(path=path, environ={})


def test_the_default_is_caldav(conn, secrets):
    target = targeting.build_target(conn, secrets=secrets)
    assert type(target).__name__ == "CalDavTarget"


def test_out_wins_over_everything(conn, tmp_path, secrets):
    """`--out` is the "show me, in files I can read" escape hatch.

    It must not depend on the configured target being reachable, or the one
    tool for inspecting a broken deployment needs the deployment to work.
    """
    from calsync.settings import set_setting

    set_setting(conn, "target_kind", "google")
    target = targeting.build_target(conn, out_dir=str(tmp_path / "out"), secrets=secrets)
    assert type(target).__name__ == "IcsFileTarget"


def test_an_unknown_kind_is_refused_by_name(conn, secrets):
    with pytest.raises(TargetError, match="unknown target kind"):
        targeting.build_target(conn, kind="carrier-pigeon", secrets=secrets)


def test_ics_file_without_a_directory_says_which_flag_is_missing(conn, secrets):
    with pytest.raises(TargetError, match="--out"):
        targeting.build_target(conn, kind="ics_file", secrets=secrets)


def test_google_refuses_early_and_says_what_is_missing(conn, secrets):
    """It used to build fine and then raise from the middle of the write loop.

    The payload builder is complete and tested; the transport does not exist.
    Failing at selection time means a half-written season is not the way you
    find out.
    """
    with pytest.raises(TargetError) as raised:
        targeting.build_target(conn, kind="google", secrets=secrets)

    message = str(raised.value)
    assert "OAuth" in message, "does not say what is actually missing"
    assert "google_calendar_map" in message


def test_google_is_still_listed_as_a_kind():
    """Pretending it does not exist would mislead as much as pretending it works."""
    assert "google" in targeting.KINDS
