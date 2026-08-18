"""What a container does on its very first start.

Two things used to have to be remembered by hand, in order, before
`docker compose up -d`: setting `radicale_url` to the compose service name, and
running `check` to prove it. Forgetting either produced a stack that came up
healthy, wrote nothing, reported it once per event and then backed off to
three-hourly — which is how a real deployment went unnoticed for days.

So the value is declared where the service name lives (the compose file, as an
environment variable) and the poller asks the question itself.
"""

from __future__ import annotations

import pytest

from calsync import cli, db
from calsync.settings import Settings, set_setting
from calsync.targets import TargetError
from calsync.targets.http import Response


# --- seeding settings from the environment ----------------------------------


def test_an_env_var_seeds_the_setting_on_a_fresh_database(tmp_path, monkeypatch):
    monkeypatch.setenv("CALSYNC_SETTING_RADICALE_URL", "http://radicale:5232")
    conn = db.open_db(tmp_path / "c.db")
    assert Settings.load(conn).radicale_url == "http://radicale:5232"


def test_it_seeds_and_never_overrides(tmp_path, monkeypatch, capsys):
    """The asymmetry is the whole design.

    A variable that reasserted itself on every restart would silently undo an
    edit made in the console, and the console is where a person looks. So the
    stored value wins — and the mismatch is said out loud, because a variable
    that quietly does nothing is its own kind of trap.
    """
    path = tmp_path / "c.db"
    monkeypatch.setenv("CALSYNC_SETTING_RADICALE_URL", "http://radicale:5232")
    conn = db.open_db(path)
    set_setting(conn, "radicale_url", "http://elsewhere:5232")
    conn.commit()
    conn.close()

    conn = db.open_db(path)
    assert Settings.load(conn).radicale_url == "http://elsewhere:5232"
    assert "http://radicale:5232" in capsys.readouterr().err


def test_an_unknown_key_refuses_rather_than_being_ignored(tmp_path, monkeypatch):
    """A typo'd variable that does nothing is the failure being removed here."""
    monkeypatch.setenv("CALSYNC_SETTING_RADICAL_URL", "http://radicale:5232")
    with pytest.raises(ValueError, match="names no setting"):
        db.open_db(tmp_path / "c.db")


def test_an_empty_value_is_not_configuration(monkeypatch):
    """Compose passes every variable it declares, filled in or not.

    Seeding the empty string would replace a working default — `default_tz`,
    `title_template` — with nothing, on a deployment that set neither.
    """
    monkeypatch.setenv("CALSYNC_SETTING_DEFAULT_TZ", "")
    assert db.settings_from_env() == {}


def test_other_calsync_variables_are_left_alone(monkeypatch):
    monkeypatch.setenv("CALSYNC_SECRET_P360_TOKEN", "shh")
    monkeypatch.setenv("CALSYNC_DB", "/data/calsync.db")
    assert db.settings_from_env() == {}


# --- the poller's startup check ---------------------------------------------


class Args:
    def __init__(self, **kw):
        self.out = None
        self.target = None
        self.__dict__.update(kw)


class Store:
    def get(self, ref):
        return "hunter2"


def _refusing(method, url, *, body=None, headers=None):
    raise TargetError("[Errno 111] Connection refused")


def _answering(method, url, *, body=None, headers=None):
    return Response(status=200, headers={})


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "c.db")
    set_setting(connection, "radicale_url", "http://radicale:5232")
    return connection


def _check(conn, monkeypatch, transport, **args):
    from calsync import targeting

    monkeypatch.setattr(
        targeting, "HttpTransport", lambda **kw: transport
    )
    return cli._startup_check(conn, Args(**args), Store())


def test_a_first_run_that_cannot_reach_its_calendar_refuses_to_start(
    conn, monkeypatch, capsys
):
    assert _check(conn, monkeypatch, _refusing) is False
    assert "refusing to start" in capsys.readouterr().err


def test_a_first_run_that_can_reach_its_calendar_starts(conn, monkeypatch):
    assert _check(conn, monkeypatch, _answering) is True


def test_a_deployment_that_has_synced_before_only_gets_a_warning(
    conn, monkeypatch, capsys
):
    """By then the address is known to have worked.

    A failure here is far likelier to be Radicale still starting than a
    misconfiguration, and exiting would take the poller off the air for an
    outage the sync loop already handles with backoff.
    """
    conn.execute("INSERT INTO children (id, name, initial) VALUES ('k', 'Kid', 'K')")
    conn.execute(
        "INSERT INTO activities (id, child_id, name, sport_id, tz) "
        "VALUES ('a', 'k', 'Otters', 'soccer', 'UTC')"
    )
    conn.execute(
        "INSERT INTO sources (id, activity_id, kind, shape) "
        "VALUES ('s', 'a', 'teamreach', 'feed')"
    )
    conn.execute("INSERT INTO poll_runs (source_id, status) VALUES ('s', 'ok')")
    conn.commit()

    assert _check(conn, monkeypatch, _refusing) is True
    assert "polling anyway" in capsys.readouterr().err


def test_writing_ics_files_has_no_server_to_ask(conn, monkeypatch):
    """`--out` does not use the configured target, so checking it is wrong."""
    assert _check(conn, monkeypatch, _refusing, out="./out") is True
    assert _check(conn, monkeypatch, _refusing, target="ics_file") is True
