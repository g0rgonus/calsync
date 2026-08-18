"""Taking a finished season off the calendars.

The failure this guards against is the obvious way to do it: delete the source
row. `event_state` cascades with it, calsync forgets it ever wrote those events,
and they stay in the shared calendar with nothing tracking them — the
duplicate-in-a-shared-calendar failure, arrived at by tidying up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from calsync import db, repo, retire
from calsync.sync import sync_source
from calsync.targets import TargetError, build

FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_comets_sample.ics"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('millie', 'Millie', 'M', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('millie-soccer-comets', 'millie', 'Comets', 'soccer',
                     'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('tr-comets', 'millie-soccer-comets', 'teamreach', 'feed');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def target(tmp_path):
    return build("ics_file", directory=tmp_path / "out")


@pytest.fixture
def synced(conn, target):
    source = repo.get_source(conn, "tr-comets")
    report = sync_source(conn, source, target, now=NOW, raw=FIXTURE.read_bytes())
    assert report.created > 0
    return report


class Refusing:
    """A target that will not let go of anything."""

    def __init__(self, after=0):
        self.after = after
        self.cancelled = 0

    def cancel(self, _ref):
        if self.cancelled >= self.after:
            raise TargetError("the calendar said no")
        self.cancelled += 1

    def ensure_collection(self, _c):
        pass


def test_retiring_cancels_every_live_event(conn, target, synced, tmp_path):
    report = retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=NOW)

    assert report.ok
    assert report.cancelled == synced.created
    assert all(s.cancelled for s in repo.event_states(conn, "tr-comets").values())
    assert not list((tmp_path / "out").rglob("*.ics")), "events left on the calendar"


def test_retiring_stops_the_polling(conn, target, synced):
    retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=NOW)
    assert not repo.get_source(conn, "tr-comets").enabled
    assert repo.list_sources(conn, enabled_only=True) == []


def test_cancelling_without_disabling_would_undo_itself(conn, target, synced):
    """Why retiring has to do both, demonstrated rather than asserted in prose.

    `known_hashes` excludes cancelled rows, so a source that is cancelled but
    still enabled sees every event in its feed as new on the very next poll and
    puts the whole season straight back.
    """
    retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=NOW)
    repo.set_enabled(conn, "tr-comets", True)

    again = sync_source(conn, repo.get_source(conn, "tr-comets"), target,
                        now=NOW, raw=FIXTURE.read_bytes())

    assert again.created == synced.created, (
        "re-enabling a retired source restored the entire season, which is "
        "exactly why retire_source disables it in the same breath"
    )


def test_a_target_that_refuses_leaves_the_source_enabled(conn, synced):
    """One unreachable event must not strand the other forty.

    Polling stays on so a later run retries, and nothing is marked gone that is
    still on somebody's phone.
    """
    report = retire.retire_source(conn, repo.get_source(conn, "tr-comets"), Refusing(),
                                  now=NOW)

    assert not report.ok
    assert not report.disabled
    assert repo.get_source(conn, "tr-comets").enabled
    assert repo.tracked_events(conn, "tr-comets") == synced.created


def test_a_partial_failure_still_records_what_did_come_off(conn, synced):
    """State follows the target, per event, in that order."""
    report = retire.retire_source(conn, repo.get_source(conn, "tr-comets"),
                                  Refusing(after=3), now=NOW)

    assert report.cancelled == 3
    assert report.errors
    assert repo.tracked_events(conn, "tr-comets") == synced.created - 3


def test_retiring_twice_is_harmless(conn, target, synced):
    retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=NOW)
    second = retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=NOW)

    assert second.ok
    assert second.cancelled == 0
    assert second.already_gone == synced.created


# --- forgetting -------------------------------------------------------------


def test_a_source_with_live_events_cannot_be_forgotten(conn, synced):
    """The whole point. Deleting the row strands every event permanently."""
    with pytest.raises(ValueError, match="Retire it first"):
        retire.forget_source(conn, "tr-comets", now=NOW)

    assert repo.get_source(conn, "tr-comets") is not None


def test_a_retired_source_can_be_forgotten(conn, target, synced):
    retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=NOW)
    retire.forget_source(conn, "tr-comets", now=NOW)

    assert repo.get_source(conn, "tr-comets") is None
    assert repo.event_states(conn, "tr-comets") == {}


def test_the_tombstones_survive_retirement(conn, target, synced):
    """They are the record that these events were ours.

    Kept so a UID that reappears next season is recognised rather than adopted
    a second time.
    """
    retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=NOW)

    states = repo.event_states(conn, "tr-comets")
    assert len(states) == synced.created
    assert all(s.cancelled for s in states.values())


# --- a finished season is not clutter ---------------------------------------
#
# The comets fixture runs 5 March to 16 May 2026. `NOW` sits before all of it,
# so every test above retires a season that has not happened yet — which is the
# mid-season case, and the one where removing events is right.

#: A month past the last fixture: what `dormancy.py` calls a finished season,
#: and the point at which `seasonend.py` nudges you towards this button.
AFTER = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)

#: Mid-season, with roughly half the fixtures played.
MIDWAY = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)


def test_retiring_a_finished_season_keeps_every_event(conn, target, synced, tmp_path):
    """The whole point. A season that was played is a record, not clutter.

    By the time anything suggests retiring, every event is in the past — the
    nudge fires a month after the last one — so this is what retiring a finished
    season now does, and it must not take last spring off the family's calendar.
    """
    before = sorted(p.name for p in (tmp_path / "out").rglob("*.ics"))

    report = retire.retire_source(conn, repo.get_source(conn, "tr-comets"),
                                  target, now=AFTER)

    assert report.ok
    assert report.cancelled == 0
    assert report.kept == synced.created
    assert sorted(p.name for p in (tmp_path / "out").rglob("*.ics")) == before
    assert not any(s.cancelled for s in repo.event_states(conn, "tr-comets").values())


def test_retiring_a_finished_season_still_stops_the_polling(conn, target, synced):
    """Which is the thing that was actually wanted."""
    report = retire.retire_source(conn, repo.get_source(conn, "tr-comets"),
                                  target, now=AFTER)

    assert report.disabled
    assert not repo.get_source(conn, "tr-comets").enabled


def test_retiring_midway_removes_what_is_left_and_keeps_what_happened(
    conn, target, synced
):
    """Both halves of the rule in one run, on one real season."""
    report = retire.retire_source(conn, repo.get_source(conn, "tr-comets"),
                                  target, now=MIDWAY)

    assert report.cancelled > 0, "nothing was upcoming, so this proves nothing"
    assert report.kept > 0, "nothing had happened, so this proves nothing"
    assert report.cancelled + report.kept == synced.created

    for state in repo.event_states(conn, "tr-comets").values():
        played = datetime.fromisoformat(state.starts_at) < MIDWAY
        assert state.cancelled is not played, (
            f"{state.starts_at} was {'kept' if played else 'removed'} wrongly"
        )


def test_the_report_says_what_was_left_alone(conn, target, synced):
    """"Retired, 0 events removed" reads like a failure. It is a whole season
    being kept, and the line has to say so."""
    line = retire.retire_source(conn, repo.get_source(conn, "tr-comets"),
                                target, now=AFTER).line()

    assert "0 cancelled" in line
    assert f"{synced.created} past events kept" in line


def test_past_events_do_not_block_forgetting(conn, target, synced):
    """Kept events are not a reason to keep the row forever.

    Nothing will ever poll this feed again, so there is nothing left for calsync
    to do to a game played in April — where an *upcoming* event would be
    stranded with nothing able to move it.
    """
    retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=AFTER)
    retire.forget_source(conn, "tr-comets", now=AFTER)

    assert repo.get_source(conn, "tr-comets") is None


def test_an_event_still_to_come_blocks_forgetting(conn, synced):
    """The guard that matters, stated against the clock rather than the count."""
    with pytest.raises(ValueError, match="still to come"):
        retire.forget_source(conn, "tr-comets", now=NOW)

    assert repo.get_source(conn, "tr-comets") is not None


def test_a_kept_season_is_not_restored_by_re_enabling(conn, target, synced):
    """The mirror of `test_cancelling_without_disabling_would_undo_itself`.

    Uncancelled past events could in principle be re-created by a later poll.
    They are not: both sides of the diff are filtered to the sync window, so an
    event old enough to be kept is invisible to it from either direction.
    """
    retire.retire_source(conn, repo.get_source(conn, "tr-comets"), target, now=AFTER)
    repo.set_enabled(conn, "tr-comets", True)

    again = sync_source(conn, repo.get_source(conn, "tr-comets"), target,
                        now=AFTER, raw=FIXTURE.read_bytes())

    assert again.created == 0, "a kept season was written to the calendar a second time"
    assert again.cancelled == 0, "a kept season was cancelled by a later poll"
