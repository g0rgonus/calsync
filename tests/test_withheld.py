"""Taking an event off the calendar and keeping it off.

The failure these exist against is quiet and specific: a deleted event comes
back. The mirror keeps no state file, so an event deleted in Calendar.app is
indistinguishable from one that was never written; the feed goes on publishing
the fixture indefinitely, so a poll puts it back even if the deletion had
reached Radicale. Every test here is about the second half — not "does it come
off" but "does it stay off across the polls that follow".

Driven through the real sync loop against an `ics_file` target, so what is on
the calendar is what is on the disk, and "still off" is an assertion about a
file that is not there.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from calsync import db, repo, warmup, withheld
from calsync.sync import sync_source
from calsync.targets import TargetError, build

FIXTURE = Path(__file__).parent / "fixtures" / "player360_sample.ics"
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('jesse', 'Jesse', 'J', 1);
        INSERT INTO activities (id, child_id, name, sport_id, official_name,
                                league, age_group, tz, alarm_game_min,
                                alarm_practice_min, warmup_minutes)
             VALUES ('jesse-soccer-vanguard', 'jesse', 'Vanguard', 'soccer', 'U10PL',
                     'PSL', 'U10', 'America/New_York', 90, 30, 0);
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('p360-jesse-vanguard', 'jesse-soccer-vanguard', 'player360', 'feed');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def target(tmp_path):
    return build("ics_file", directory=tmp_path / "out")


def _sync(conn, target, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("raw", FIXTURE.read_bytes())
    return sync_source(conn, repo.list_sources(conn)[0], target, **kwargs)


def _on_calendar(tmp_path) -> set[str]:
    return {path.stem for path in (tmp_path / "out").rglob("*.ics")}


def _an_upcoming_uid(conn) -> str:
    """Any uid this source has written that has not happened yet."""
    states = repo.event_states(conn, "p360-jesse-vanguard")
    live = sorted(
        uid for uid, state in states.items()
        if not state.cancelled and state.starts_at > NOW.isoformat()
    )
    assert live, "the fixture should leave something upcoming to withhold"
    return live[0]


# --- the decision sticks ----------------------------------------------------


@pytest.mark.parametrize("reason", sorted(withheld.REASONS))
def test_a_withheld_event_comes_off_the_calendar(conn, target, tmp_path, reason):
    _sync(conn, target)
    uid = _an_upcoming_uid(conn)
    assert uid in _on_calendar(tmp_path)

    repo.set_withheld(conn, uid, reason)
    conn.commit()
    report = withheld.enforce(conn, "p360-jesse-vanguard", target)

    assert report.removed == 1
    assert uid not in _on_calendar(tmp_path)


def test_the_next_poll_does_not_put_it_back(conn, target, tmp_path):
    """The whole point. The feed still publishes it, and will forever."""
    _sync(conn, target)
    uid = _an_upcoming_uid(conn)
    repo.set_withheld(conn, uid, "not_attending")
    conn.commit()
    withheld.enforce(conn, "p360-jesse-vanguard", target)

    for _ in range(3):
        report = _sync(conn, target)
        assert report.status == "ok"
        assert uid not in _on_calendar(tmp_path), "the poll put it back"


def test_the_poll_removes_it_when_the_calendar_was_unreachable(conn, target, tmp_path):
    """The console saves the decision even if it cannot act on it, so the sync
    has to be the thing that makes it true."""
    _sync(conn, target)
    uid = _an_upcoming_uid(conn)
    repo.set_withheld(conn, uid, "cancelled")
    conn.commit()

    report = _sync(conn, target)

    assert uid not in _on_calendar(tmp_path)
    assert report.cancelled >= 1


def test_clearing_the_decision_puts_it_back(conn, target, tmp_path):
    """Restoring writes no event of its own: it clears the flag and lets an
    ordinary poll create it, because `known_hashes` skips the cancelled row."""
    _sync(conn, target)
    uid = _an_upcoming_uid(conn)
    repo.set_withheld(conn, uid, "not_attending")
    conn.commit()
    _sync(conn, target)
    assert uid not in _on_calendar(tmp_path)

    repo.set_withheld(conn, uid, None)
    conn.commit()
    report = _sync(conn, target)

    assert uid in _on_calendar(tmp_path)
    assert report.created >= 1
    assert not repo.event_state(conn, uid).cancelled


# --- what it must not disturb ----------------------------------------------


def test_withholding_leaves_every_other_event_alone(conn, target, tmp_path):
    _sync(conn, target)
    before = _on_calendar(tmp_path)
    uid = _an_upcoming_uid(conn)

    repo.set_withheld(conn, uid, "cancelled")
    conn.commit()
    _sync(conn, target)

    assert _on_calendar(tmp_path) == before - {uid}


def test_a_withheld_event_is_not_a_disappearance(conn, target):
    """It is still in the feed, so it must not count toward the guard — a
    handful of standing exclusions must never read as a truncated fetch."""
    _sync(conn, target)
    states = repo.event_states(conn, "p360-jesse-vanguard")
    upcoming = sorted(
        uid for uid, state in states.items()
        if not state.cancelled and state.starts_at > NOW.isoformat()
    )
    for uid in upcoming:
        repo.set_withheld(conn, uid, "not_attending")
    conn.commit()

    report = _sync(conn, target)

    assert report.status == "ok"
    assert report.held is None


# --- the warm-up follows its game ------------------------------------------


def _with_warmups(conn, minutes=45):
    conn.execute("UPDATE activities SET warmup_minutes = ?", (minutes,))
    conn.commit()


def _a_game_with_a_warm_up(conn, tmp_path) -> tuple[str, str]:
    """An upcoming game whose warm-up is on the calendar, and that warm-up."""
    states = repo.event_states(conn, "p360-jesse-vanguard")
    written = _on_calendar(tmp_path)
    for uid in sorted(states):
        if warmup.is_synthetic(uid) or states[uid].starts_at <= NOW.isoformat():
            continue
        if warmup.uid_for(uid) in written:
            return uid, warmup.uid_for(uid)
    raise AssertionError("the fixture should have produced a warm-up to check")


def test_withholding_a_game_withholds_the_warm_up_in_front_of_it(
    conn, target, tmp_path
):
    """Otherwise you keep the 45 minutes of getting there for a game nobody is
    going to."""
    _with_warmups(conn)
    _sync(conn, target)
    game, warm = _a_game_with_a_warm_up(conn, tmp_path)

    repo.set_withheld(conn, game, "not_attending")
    conn.commit()
    _sync(conn, target)

    assert game not in _on_calendar(tmp_path)
    assert warm not in _on_calendar(tmp_path)
    # Derived, never stored: one decision is one row.
    assert repo.event_state(conn, warm).withheld is None


def test_restoring_a_game_brings_its_warm_up_back(conn, target, tmp_path):
    _with_warmups(conn)
    _sync(conn, target)
    game, warm = _a_game_with_a_warm_up(conn, tmp_path)

    repo.set_withheld(conn, game, "not_attending")
    conn.commit()
    _sync(conn, target)

    repo.set_withheld(conn, game, None)
    conn.commit()
    _sync(conn, target)

    assert {game, warm} <= _on_calendar(tmp_path)


# --- the column holds a reason, not free text -------------------------------


def test_an_unrecognised_reason_is_refused(conn, target):
    _sync(conn, target)
    uid = _an_upcoming_uid(conn)
    with pytest.raises(ValueError):
        repo.set_withheld(conn, uid, "maybe")


def test_a_failed_removal_keeps_the_flag_so_a_later_sync_retries(conn, target, tmp_path):
    _sync(conn, target)
    uid = _an_upcoming_uid(conn)
    repo.set_withheld(conn, uid, "cancelled")
    conn.commit()

    class Unreachable:
        def cancel(self, _ref):
            raise TargetError("the calendar server is down")

    report = withheld.enforce(conn, "p360-jesse-vanguard", Unreachable())

    assert not report.ok
    assert repo.event_state(conn, uid).withheld == "cancelled"
    assert not repo.event_state(conn, uid).cancelled

    # And the next sync, against a calendar that answers, finishes the job.
    _sync(conn, target)
    assert uid not in _on_calendar(tmp_path)
