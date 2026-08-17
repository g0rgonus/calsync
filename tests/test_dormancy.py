"""Spotting a season that has ended — and, mostly, refusing to.

Every test here that asserts `suspected is False` is doing the real work. A
finished season and a broken feed are indistinguishable from inside calsync, so
the interesting behaviour is how hard this declines to guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from calsync import db, dormancy
from calsync.dormancy import assess

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
LONG_AGO = NOW - timedelta(days=40)
RECENTLY = NOW - timedelta(days=2)


def verdict(**overrides):
    kwargs = dict(
        source_id="tr-comets",
        consecutive_errors=9,
        last_success_at=LONG_AGO,
        upcoming_events=0,
        now=NOW,
    )
    kwargs.update(overrides)
    return assess(**kwargs)


def test_a_dead_season_is_recognised():
    """Failing for weeks, nothing upcoming — what a replaced team looks like."""
    result = verdict()
    assert result.suspected
    assert "end of a season" in result.reason
    assert "nothing has been changed" in result.reason


# --- the three innocent explanations ----------------------------------------


def test_a_host_outage_is_not_a_dead_season():
    """Failing hard, but it worked on Friday."""
    assert not verdict(last_success_at=RECENTLY).suspected


def test_a_gap_between_seasons_is_not_a_dead_season():
    """A healthy feed with nothing scheduled yet. March, before the fixtures."""
    assert not verdict(consecutive_errors=0).suspected


def test_a_quiet_winter_with_events_still_upcoming_is_not_a_dead_season():
    assert not verdict(upcoming_events=4).suspected


def test_one_bad_afternoon_is_not_enough():
    """Five fast failures inside a day is a wobble, not a season ending."""
    assert not verdict(consecutive_errors=3).suspected


# --- it only ever labels ----------------------------------------------------


def test_the_verdict_carries_no_action():
    """Nothing here can retire, disable, cancel or delete.

    Asserted on the type: a finished season and a broken feed look identical
    from in here, so acting on the guess is the one thing that must stay
    impossible.
    """
    result = verdict()
    assert isinstance(result, dormancy.Verdict)
    assert not any(
        hasattr(result, name) for name in ("apply", "retire", "disable", "execute")
    )


# --- reading it out of the database -----------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('millie', 'Millie', 'M', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('a', 'millie', 'Comets', 'soccer', 'UTC');
        INSERT INTO sources (id, activity_id, kind, shape) VALUES ('s', 'a', 'teamreach', 'feed');
        """
    )
    connection.commit()
    return connection


def _poll(conn, status, n=1):
    for _ in range(n):
        conn.execute("INSERT INTO poll_runs (source_id, status) VALUES (?, ?)", ("s", status))
    conn.commit()


def test_consecutive_errors_stop_at_the_last_success(conn):
    _poll(conn, "error", 4)
    _poll(conn, "ok")
    _poll(conn, "error", 3)

    assert dormancy.consecutive_errors(conn, "s") == 3


def test_a_held_poll_breaks_the_run(conn):
    """Held means the feed answered. That is not a source going dark."""
    _poll(conn, "error", 2)
    _poll(conn, "held")
    _poll(conn, "error", 1)

    assert dormancy.consecutive_errors(conn, "s") == 1


def test_upcoming_events_ignore_the_past_and_the_cancelled(conn):
    conn.executescript(
        """
        INSERT INTO event_state (uid, source_id, collection, content_hash, starts_at)
             VALUES ('past', 's', 'games', 'h', '2026-01-01T00:00:00+00:00'),
                    ('soon', 's', 'games', 'h', '2026-12-01T00:00:00+00:00');
        INSERT INTO event_state (uid, source_id, collection, content_hash, starts_at, cancelled)
             VALUES ('gone', 's', 'games', 'h', '2026-12-02T00:00:00+00:00', 1);
        """
    )
    conn.commit()

    assert dormancy.upcoming_events(conn, "s", now=NOW) == 1


def test_a_source_that_has_never_polled_is_not_suspected(conn):
    """A brand new source has no successes and nothing upcoming by definition."""
    assert not dormancy.for_source(conn, "s", now=NOW).suspected


def test_the_whole_thing_end_to_end(conn):
    _poll(conn, "error", 6)
    conn.execute("UPDATE sources SET last_success_at = ? WHERE id = 's'",
                 (LONG_AGO.isoformat(),))
    conn.commit()

    result = dormancy.for_source(conn, "s", now=NOW)
    assert result.suspected
    assert result.consecutive_errors == 6
    assert result.days_since_success == 40
