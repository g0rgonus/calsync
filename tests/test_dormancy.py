"""Spotting a season that has ended.

The first version of this looked for consecutive fetch failures, which was
simply wrong about the world: a rec team's app goes on serving last spring's
fixtures with a clean 200 forever, so a finished season never fails at all and
the detector could never have fired for the case it was written for.

The tell is the dates — nothing new published for months, nothing upcoming.
Several tests below exist only to keep that mistake from coming back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from calsync import db, dormancy
from calsync.dormancy import assess

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
LAST_SPRING = NOW - timedelta(days=100)
A_FORTNIGHT_AGO = NOW - timedelta(days=14)


def verdict(**overrides):
    kwargs = dict(
        source_id="tr-wrens",
        last_event_at=LAST_SPRING,
        upcoming_events=0,
        now=NOW,
    )
    kwargs.update(overrides)
    return assess(**kwargs)


def test_a_season_that_ended_in_spring_is_recognised_in_august():
    result = verdict()
    assert result.suspected
    assert result.days_since_last_event == 100
    assert "100 days ago" in result.reason
    assert "Nothing on the calendar has been changed" in result.reason


def test_a_healthy_feed_is_the_normal_case_and_does_not_hide_it():
    """The feed answering is not evidence of life, and the copy says so.

    This is the whole correction: a team app serves a finished season happily
    and indefinitely.
    """
    result = verdict(consecutive_errors=0)
    assert result.suspected
    assert "still answers fine" in result.reason


def test_failures_are_reported_but_gate_nothing():
    """Requiring them was the original bug. They are context, not a condition."""
    with_errors = verdict(consecutive_errors=7)
    assert with_errors.suspected
    assert "failed to fetch 7 times" in with_errors.reason
    assert verdict(consecutive_errors=0).suspected, "errors became required again"


# --- the innocent explanations ----------------------------------------------


def test_anything_upcoming_means_the_season_is_running():
    assert not verdict(upcoming_events=1).suspected


def test_a_recent_last_event_is_a_lull_not_an_ending():
    """March: practices have run, the coach has not posted fixtures yet."""
    assert not verdict(last_event_at=A_FORTNIGHT_AGO, upcoming_events=0).suspected


def test_a_long_quiet_source_is_flagged_whether_or_not_it_will_return():
    """A club team kept across seasons goes quiet every summer and comes back.

    It is still flagged — a quiet feed is worth knowing about either way — and
    `seasonend` is what declines to switch that one off. Splitting it this way
    keeps the observation honest and the action cautious.
    """
    assert verdict(last_event_at=NOW - timedelta(days=80)).suspected


def test_a_brand_new_source_is_not_a_finished_one():
    """Nothing written yet looks identical to nothing written for a year."""
    assert not verdict(last_event_at=None).suspected


# --- it only ever labels ----------------------------------------------------


def test_the_verdict_carries_no_action():
    result = verdict()
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
             VALUES ('mira', 'Mira', 'M', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('a', 'mira', 'Wrens', 'soccer', 'UTC');
        INSERT INTO sources (id, activity_id, kind, shape) VALUES ('s', 'a', 'teamreach', 'feed');
        """
    )
    connection.commit()
    return connection


def _event(conn, uid, starts_at, cancelled=0):
    conn.execute(
        "INSERT INTO event_state (uid, source_id, collection, content_hash, starts_at, cancelled)"
        " VALUES (?, 's', 'games', 'h', ?, ?)",
        (uid, starts_at.isoformat(), cancelled),
    )
    conn.commit()


def test_the_last_event_comes_from_stored_state_not_a_fetch(conn):
    """Answering this must not cost a round trip per source per page load."""
    _event(conn, "old", NOW - timedelta(days=200))
    _event(conn, "newest", LAST_SPRING)

    assert dormancy.last_event_at(conn, "s") == LAST_SPRING


def test_a_cancelled_final_fixture_still_counts(conn):
    """A season whose last act was calling off its final game is just as over."""
    _event(conn, "called-off", LAST_SPRING, cancelled=1)

    assert dormancy.last_event_at(conn, "s") == LAST_SPRING
    assert dormancy.for_source(conn, "s", now=NOW).suspected


def test_upcoming_events_ignore_the_past_and_the_cancelled(conn):
    _event(conn, "past", NOW - timedelta(days=10))
    _event(conn, "soon", NOW + timedelta(days=10))
    _event(conn, "gone", NOW + timedelta(days=11), cancelled=1)

    assert dormancy.upcoming_events(conn, "s", now=NOW) == 1


def test_a_source_that_has_never_synced_is_not_suspected(conn):
    assert not dormancy.for_source(conn, "s", now=NOW).suspected


def test_the_whole_thing_end_to_end_on_a_feed_that_never_failed(conn):
    """The real shape: a clean, working feed for a team that no longer exists."""
    for n in range(6):
        conn.execute("INSERT INTO poll_runs (source_id, status) VALUES ('s', 'ok')")
    _event(conn, "final", LAST_SPRING)
    conn.commit()

    result = dormancy.for_source(conn, "s", now=NOW)
    assert result.suspected
    assert result.consecutive_errors == 0
    assert result.days_since_last_event == 100
