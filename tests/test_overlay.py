"""A person's correction to one event, and the poll that must not undo it.

The property under test throughout is the layering in `overlay.py`: a poll
writes the source layer and a person writes the overlay, so the feed going on
publishing the old value changes nothing — and the diff, the guard and
`event_content` go on seeing only what the feed said.

The second property is that a correction reaches the calendar although the
feed's hash never moves, and reaches it exactly once. Too few writes and the
correction never lands on a phone; too many and a shared calendar is rewritten
on every poll, which the Mac mirror's timezone churn showed is not free.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from calsync import db, digest, overlay, repo, retire, warmup
from calsync.api import app as api_app
from calsync.models import Event, Venue
from calsync.overlay import OverrideError
from calsync.sync import sync_source
from calsync.targets import TargetRef

FIXTURE = Path(__file__).parent / "fixtures" / "player360_sample.ics"
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
SOURCE = "p360-jesse-vanguard"

#: "U10PL PSL Match vs Harbour FC U10", 10:00 Eastern on Saturday 29 August.
GAME = "360Player-event-4823901"
GAME_START = "DTSTART:20260829T140000Z"
WARMUP = warmup.uid_for(GAME)
#: A practice, which gets no warm-up — so a correction to it is one write.
PRACTICE = "360Player-event-4801208"

MOVED = {
    "starts_at": "2026-08-29T15:00:00+00:00",
    "ends_at": "2026-08-29T16:00:00+00:00",
}


class RecordingTarget:
    """A calendar that remembers every write, so a test can count them."""

    def __init__(self):
        self.calendar = {}
        self.writes = []
        self.cancelled = []

    def ensure_collection(self, _collection):
        pass

    def upsert(self, event, _previous=None):
        self.calendar[event.uid] = event
        self.writes.append(event.uid)
        return TargetRef(collection=event.collection, remote_id=event.uid, etag='"1"')

    def cancel(self, ref):
        self.cancelled.append(ref.remote_id)
        self.calendar.pop(ref.remote_id, None)

    def take(self) -> list[str]:
        """The writes since the last call, and forget them."""
        writes, self.writes = self.writes, []
        return writes


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('jesse', 'Jesse', 'J', 1);
        INSERT INTO activities (id, child_id, name, sport_id, official_name,
                                league, age_group, tz, warmup_minutes)
             VALUES ('jesse-soccer-vanguard', 'jesse', 'Vanguard', 'soccer',
                     'U10PL', 'PSL', 'U10', 'America/New_York', 45);
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('p360-jesse-vanguard', 'jesse-soccer-vanguard', 'player360', 'feed');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def target():
    return RecordingTarget()


@pytest.fixture
def synced(conn, target):
    """A season on the calendar, and the writes that put it there forgotten."""
    report = _sync(conn, target)
    assert report.status == "ok", report.line()
    assert GAME in target.calendar and WARMUP in target.calendar
    target.take()
    return target


def _sync(conn, target, *, raw=None, now=NOW):
    source = repo.get_source(conn, SOURCE)
    return sync_source(
        conn, source, target, now=now,
        raw=raw if raw is not None else FIXTURE.read_bytes(),
    )


def _override(conn, uid, unit, value, reason="the coach texted"):
    repo.set_override(conn, uid, unit, value, reason=reason, actor="dan")
    conn.commit()


def _feed_with(old: str, new: str) -> bytes:
    text = FIXTURE.read_text()
    assert text.count(old) == 1, old
    return text.replace(old, new).encode()


# --- it survives ------------------------------------------------------------


def test_an_override_survives_every_poll(conn, synced):
    _override(conn, GAME, "time", MOVED)

    report = _sync(conn, synced)
    # The game, and its warm-up, which ends at the kick-off that moved.
    assert sorted(synced.take()) == sorted([GAME, WARMUP])
    assert report.amended == 1
    assert synced.calendar[GAME].starts_at.isoformat() == MOVED["starts_at"]

    # The feed goes on saying 14:00, and nothing goes back.
    for _ in range(3):
        report = _sync(conn, synced)
        assert synced.take() == []
        assert report.amended == 0
    assert synced.calendar[GAME].starts_at.isoformat() == MOVED["starts_at"]


def test_setting_editing_and_lifting_each_rewrite_once(conn, synced):
    """Against a feed that has not changed at all, which is the whole point:
    the hash never moves, and the correction still has to reach a phone."""
    feed_notes = synced.calendar[PRACTICE].body

    _override(conn, PRACTICE, "notes", {"body": "Bring the pinnies"})
    _sync(conn, synced)
    assert synced.take() == [PRACTICE]
    assert "Bring the pinnies" in synced.calendar[PRACTICE].body

    _override(conn, PRACTICE, "notes", {"body": "Bring the pinnies and water"})
    _sync(conn, synced)
    assert synced.take() == [PRACTICE]
    assert "and water" in synced.calendar[PRACTICE].body

    repo.remove_override(conn, PRACTICE, "notes", actor="dan")
    conn.commit()
    report = _sync(conn, synced)
    assert synced.take() == [PRACTICE]
    assert report.amended == 1
    assert synced.calendar[PRACTICE].body == feed_notes


def test_a_fix_that_says_what_the_feed_says_is_lifted_without_a_write(conn, synced):
    """The sum of the layers never moves, so neither does the calendar — and
    the fix is redundant from the first poll, so it goes, and the receipts
    stop crediting a person with the feed's own value."""
    feed_detail = repo.event_contents(conn, SOURCE)[PRACTICE]["detail"]
    _override(conn, PRACTICE, "detail", {"detail": feed_detail})

    report = _sync(conn, synced)
    assert synced.take() == []
    assert report.fixes_cleared == 1
    assert repo.overrides_of(conn, PRACTICE) == {}
    assert repo.event_state(conn, PRACTICE).applied_overlay is None
    assert repo.stored_event(conn, PRACTICE).overrides is None


# --- warm-ups follow --------------------------------------------------------


def test_the_warm_up_follows_a_moved_kick_off(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)

    moved = datetime.fromisoformat(MOVED["starts_at"])
    warm = synced.calendar[WARMUP]
    assert warm.ends_at == moved
    assert warm.starts_at == moved - timedelta(minutes=45)
    # And the game states the arrival time that goes with it.
    assert "Arrive: 10:15" in synced.calendar[GAME].body
    assert repo.event_contents(conn, SOURCE)[GAME]["arrive_at"] == (
        (moved - timedelta(minutes=45)).isoformat()
    )


def test_the_warm_up_follows_a_corrected_venue(conn, synced):
    _override(conn, GAME, "location", {
        "venue_name": "Kingsmere", "venue_address": "9 Wren Rd", "venue_field": "#2",
    })
    _sync(conn, synced)

    for uid in (GAME, WARMUP):
        assert synced.calendar[uid].location_text == "Kingsmere, 9 Wren Rd"
        assert "Field: #2" in synced.calendar[uid].body


def test_a_feed_move_under_an_override_writes_the_warm_up_once(conn, synced):
    """Two versions of a warm-up's content would differ on every poll, and the
    receipt check would rewrite it forever. There is one version."""
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    synced.take()

    moved_feed = _feed_with(GAME_START, "DTSTART:20260829T143000Z")
    _sync(conn, synced, raw=moved_feed)
    writes = synced.take()
    assert writes.count(GAME) == 1 and writes.count(WARMUP) == 1

    for _ in range(3):
        _sync(conn, synced, raw=moved_feed)
        assert synced.take() == []
    # The person's correction still stands over the feed's new time.
    assert synced.calendar[GAME].starts_at.isoformat() == MOVED["starts_at"]
    assert synced.calendar[WARMUP].ends_at.isoformat() == MOVED["starts_at"]


def test_a_time_on_an_all_day_game_gives_it_a_warm_up(conn, synced):
    """The tournament day a coach entered before the kick-off existed. Once a
    person knows the time, the game is a timed game in every respect."""
    feed = _feed_with(
        GAME_START + "\nDTEND:20260829T150000Z",
        "DTSTART;VALUE=DATE:20260829\nDTEND;VALUE=DATE:20260830",
    )
    _sync(conn, synced, raw=feed)
    synced.take()
    assert WARMUP not in synced.calendar

    _override(conn, GAME, "time", {**MOVED, "all_day": 0})
    _sync(conn, synced, raw=feed)
    assert WARMUP in synced.calendar
    assert not synced.calendar[GAME].all_day

    repo.remove_override(conn, GAME, "time")
    conn.commit()
    report = _sync(conn, synced, raw=feed)
    # A warm-up is not evidence about the fetch, so its going is not a guard
    # trip — it is an ordinary cancellation of something calsync derived.
    assert report.status == "ok", report.line()
    assert WARMUP in synced.cancelled
    assert synced.calendar[GAME].all_day


# --- the source layer stays the source's ------------------------------------


def test_the_source_layer_records_only_what_the_feed_said(conn, synced):
    hash_before = repo.event_state(conn, GAME).content_hash
    venue_before = repo.event_contents(conn, SOURCE)[GAME]["venue_name"]

    _override(conn, GAME, "time", MOVED)
    _override(conn, GAME, "location", {"venue_name": "Kingsmere"})
    _sync(conn, synced)

    state = repo.event_state(conn, GAME)
    assert state.starts_at == "2026-08-29T14:00:00+00:00"
    assert state.shown_starts_at == MOVED["starts_at"]
    assert state.content_hash == hash_before
    content = repo.event_contents(conn, SOURCE)[GAME]
    assert content["venue_name"] == venue_before
    assert content["ends_at"] == "2026-08-29T15:00:00+00:00"


def test_the_diff_and_the_guard_see_the_same_feed(conn, synced):
    """Overrides on events the feed then drops are weighed like any others:
    four of seven gone is a held poll, corrected or not."""
    _override(conn, GAME, "time", MOVED)
    _override(conn, PRACTICE, "notes", {"body": "x"})
    _sync(conn, synced)

    text = FIXTURE.read_text()
    blocks = text.split("BEGIN:VEVENT")
    # Keep the header and the first event only.
    truncated = ("BEGIN:VEVENT".join(blocks[:2]).rstrip() + "\nEND:VCALENDAR\n").encode()
    report = _sync(conn, synced, raw=truncated)
    assert report.status == "held"
    assert synced.cancelled == []


def test_cancellation_wins_and_does_not_resurrect(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)

    cancelled = _feed_with(GAME_START, GAME_START + "\nSTATUS:CANCELLED")
    report = _sync(conn, synced, raw=cancelled)
    assert report.called_off == 2
    assert GAME not in synced.calendar and WARMUP not in synced.calendar
    synced.take()

    for _ in range(2):
        _sync(conn, synced, raw=cancelled)
        assert synced.take() == []
    # Kept, not discarded: if the feed reinstates the game, the correction
    # is still the person's.
    assert "time" in repo.overrides_of(conn, GAME)

    _sync(conn, synced)
    assert synced.calendar[GAME].starts_at.isoformat() == MOVED["starts_at"]


def test_withholding_wins(conn, synced):
    repo.set_withheld(conn, GAME, "not_attending")
    conn.commit()
    _sync(conn, synced)
    synced.take()

    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    assert synced.take() == []
    assert GAME not in synced.calendar


# --- readers say what is on the calendar ------------------------------------


def test_readers_compose_what_was_written_not_what_was_saved(conn, synced):
    """Between saving a correction and the sync that lands it, the receipt
    still describes the calendar — which does not have it yet."""
    _override(conn, GAME, "opponent", {"opponent": "Harbour FC Blue"})

    assert repo.stored_event(conn, GAME).event.opponent != "Harbour FC Blue"

    _sync(conn, synced)
    assert repo.stored_event(conn, GAME).event.opponent == "Harbour FC Blue"


def test_a_moved_event_is_read_on_the_day_it_was_moved_to(conn, synced):
    _override(conn, GAME, "time", {
        "starts_at": "2026-09-08T22:00:00+00:00",
        "ends_at": "2026-09-08T23:00:00+00:00",
    })
    _sync(conn, synced)

    def on(day):
        return {
            item.event.uid for item in repo.stored_events(
                conn, start=f"{day}T00:00:00+00:00", end=f"{day}T23:59:59+00:00",
            )
        }

    assert GAME in on("2026-09-08")
    assert GAME not in on("2026-08-29")
    shown = {p["uid"]: p["starts_at"] for p in repo.placements(conn, since="2026-07-01")}
    assert shown[GAME] == "2026-09-08T22:00:00+00:00"


def test_the_digest_reports_the_corrected_event(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)

    report = digest.collect(conn, now=datetime(2026, 8, 29, 4, tzinfo=timezone.utc))
    (entry,) = [e for e in report.entries if e.is_game]
    assert entry.starts_at.isoformat() == MOVED["starts_at"]


def test_the_api_says_who_owns_each_field(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)

    item = repo.stored_event(conn, GAME)
    resolution = api_app._resolution(conn, item)
    for field in ("starts_at", "ends_at"):
        assert resolution[field]["owner"] == "override"
        assert resolution[field]["tier"] == api_app.OVERRIDE_TIER
        assert resolution[field]["source_id"] is None
    for field in ("venue", "opponent", "notes"):
        assert resolution[field]["owner"] == "source"
        assert resolution[field]["source_id"] == SOURCE


def test_retiring_goes_by_when_the_event_is_on_the_calendar(conn, synced):
    """A game the feed has as yesterday and a person moved to next week is
    still to come, and retiring takes it off like any other."""
    _override(conn, GAME, "time", {
        "starts_at": "2026-09-02T14:00:00+00:00",
        "ends_at": "2026-09-02T15:00:00+00:00",
    })
    _sync(conn, synced)

    after_the_feeds_date = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
    retire.retire_source(
        conn, repo.get_source(conn, SOURCE), synced, now=after_the_feeds_date,
    )
    assert GAME in synced.cancelled


# --- what may be stored -----------------------------------------------------


@pytest.mark.parametrize("unit, value, message", [
    ("title", {"title": "x"}, "not something"),
    ("time", {"starts_at": "2026-08-29T15:00:00"}, "needs ends_at"),
    ("time", {"starts_at": "2026-08-29T15:00:00", "ends_at": "2026-08-29T16:00:00"},
     "offset"),
    ("time", {"starts_at": MOVED["ends_at"], "ends_at": MOVED["starts_at"]}, "before"),
    ("location", {"venue_name": "Kingsmere #2"}, "field within"),
    ("location", {"venue_name": "  "}, "needs the venue"),
    ("opponent", {"opponent": "x", "home": True}, "does not carry"),
])
def test_an_override_that_cannot_be_stored_is_refused(conn, synced, unit, value, message):
    with pytest.raises(OverrideError, match=message):
        repo.set_override(conn, GAME, unit, value, reason="r")


def test_a_warm_up_is_corrected_through_its_game(conn, synced):
    with pytest.raises(OverrideError, match="follows its game"):
        repo.set_override(conn, WARMUP, "time", MOVED, reason="r")


def test_a_reason_is_required(conn, synced):
    with pytest.raises(OverrideError, match="say why"):
        repo.set_override(conn, GAME, "time", MOVED, reason="  ")


def test_every_change_is_in_the_history(conn, synced):
    _override(conn, GAME, "opponent", {"opponent": "Harbour Blue"}, reason="typo")
    _override(conn, GAME, "opponent", {"opponent": "Harbour FC Blue"}, reason="typo 2")
    repo.remove_override(conn, GAME, "opponent", actor="dan", reason="fixed upstream")

    rows = repo.override_history(conn, GAME)
    assert [r["action"] for r in rows] == ["set", "edit", "remove"]
    assert json.loads(rows[0]["upstream"]) == {"opponent": "Harbour FC"}
    assert json.loads(rows[1]["old_value"]) == {"opponent": "Harbour Blue"}
    assert rows[2]["reason"] == "fixed upstream"
    assert all(r["actor"] == "dan" for r in rows)


def test_overrides_age_out_with_the_window(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)

    _sync(conn, synced, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert repo.overrides_of(conn, GAME) == {}
    assert repo.override_history(conn, GAME) == []


# --- the translation --------------------------------------------------------


def test_apply_and_project_agree():
    """Two descriptions of one correction — on an `Event` and on its receipt —
    must not drift, or the sync loop would rewrite on every poll."""
    start = datetime(2026, 8, 29, 14, tzinfo=timezone.utc)
    event = Event(
        uid="e", activity_id="a", starts_at=start, ends_at=start + timedelta(hours=1),
        is_game=True, tz="America/New_York",
        venue=Venue(raw="Thistledown", name="Thistledown"),
        opponent="Harbour", detail="Match", body="feed notes",
    )
    layer = {
        unit: {"value": overlay.normalize(unit, value), "set_at": "2026-07-20 12:00:00"}
        for unit, value in {
            "time": MOVED,
            "location": {"venue_name": "Kingsmere", "venue_field": "#2"},
            "opponent": {"opponent": "Harbour FC"},
            "detail": {"detail": None},
            "notes": {"body": "ours"},
        }.items()
    }

    corrected = overlay.apply(event, layer)
    assert {**repo.content_of(corrected), "starts_at": corrected.starts_at.isoformat()} \
        == overlay.project(repo.content_of(event), event.starts_at.isoformat(), layer)
    # And the original is untouched: it is what `event_content` records.
    assert event.opponent == "Harbour" and event.starts_at == start


def test_an_existing_database_learns_when_its_events_are_on(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order) VALUES ('c', 'C', 'C', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('a', 'c', 'A', 'soccer', 'UTC');
        INSERT INTO sources (id, activity_id, kind, shape) VALUES ('s', 'a', 'player360', 'feed');
        INSERT INTO event_state (uid, source_id, collection, content_hash, starts_at)
             VALUES ('u', 's', 'games', 'h', '2026-08-01T10:00:00+00:00');
        UPDATE event_state SET shown_starts_at = NULL;
        """
    )
    conn.commit()
    conn.close()

    conn = db.open_db(path)
    assert repo.event_state(conn, "u").shown_starts_at == "2026-08-01T10:00:00+00:00"
    row = conn.execute("SELECT shown_starts_at FROM event_state").fetchone()
    assert row[0] == "2026-08-01T10:00:00+00:00"


# --- the feed catches up, or moves elsewhere ---------------------------------
#
# Three outcomes per fix, judged unit by unit after every poll: the feed now
# says what the fix says (lift it, silently — nothing on the calendar changes);
# it still says what it said (leave it); or it says something else again (keep
# the fix, and ask a person which is right).



def _status(conn, unit="time"):
    item = repo.overrides_of(conn, GAME).get(unit)
    return item.status if item else None


def _actions(conn):
    return [r["action"] for r in repo.override_history(conn, GAME)]


def test_a_fix_the_feed_catches_up_with_is_lifted(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    synced.take()

    caught_up = _feed_with(GAME_START + "\nDTEND:20260829T150000Z",
                           "DTSTART:20260829T150000Z\nDTEND:20260829T160000Z")
    report = _sync(conn, synced, raw=caught_up)
    assert report.fixes_cleared == 1
    assert repo.overrides_of(conn, GAME) == {}
    assert _actions(conn)[-1] == "auto_clear"
    # The calendar already showed 15:00; the feed agreeing changes nothing on it.
    assert synced.calendar[GAME].starts_at.isoformat() == MOVED["starts_at"]
    synced.take()
    for _ in range(2):
        _sync(conn, synced, raw=caught_up)
        assert synced.take() == []


def test_a_feed_that_moves_elsewhere_keeps_the_fix_and_asks(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)

    elsewhere = _feed_with(GAME_START + "\nDTEND:20260829T150000Z",
                           "DTSTART:20260829T143000Z\nDTEND:20260829T153000Z")
    report = _sync(conn, synced, raw=elsewhere)
    assert report.fixes_changed == 1
    item = repo.overrides_of(conn, GAME)["time"]
    assert item.status == "changed"
    assert item.upstream_seen["starts_at"] == "2026-08-29T14:30:00+00:00"
    # The person's value is still what everyone sees.
    assert synced.calendar[GAME].starts_at.isoformat() == MOVED["starts_at"]

    # Asked once, not once a poll.
    for _ in range(2):
        assert _sync(conn, synced, raw=elsewhere).fixes_changed == 0
    assert _actions(conn).count("changed") == 1


def test_a_feed_that_goes_back_settles_the_question(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    elsewhere = _feed_with(GAME_START, "DTSTART:20260829T143000Z")
    _sync(conn, synced, raw=elsewhere)
    assert _status(conn) == "changed"

    _sync(conn, synced)
    item = repo.overrides_of(conn, GAME)["time"]
    assert item.status == "active" and item.upstream_seen is None
    assert _actions(conn)[-1] == "settled"


def test_keeping_the_fix_makes_the_new_value_the_baseline(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    elsewhere = _feed_with(GAME_START, "DTSTART:20260829T143000Z")
    _sync(conn, synced, raw=elsewhere)

    repo.keep_override(conn, GAME, "time", actor="dan")
    conn.commit()
    _sync(conn, synced, raw=elsewhere)
    assert _status(conn) == "active"
    # Only a further change is news.
    further = _feed_with(GAME_START, "DTSTART:20260829T144500Z")
    assert _sync(conn, synced, raw=further).fixes_changed == 1


def test_each_part_of_a_fix_is_judged_on_its_own(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _override(conn, GAME, "opponent", {"opponent": "Harbour FC Blue"})
    _sync(conn, synced)

    feed = _feed_with(GAME_START + "\nDTEND:20260829T150000Z",
                      "DTSTART:20260829T150000Z\nDTEND:20260829T160000Z")
    feed = feed.replace(b"Match vs Harbour FC U10", b"Match vs Saltmarsh U10")
    report = _sync(conn, synced, raw=feed)
    assert report.fixes_cleared == 1 and report.fixes_changed == 1
    assert set(repo.overrides_of(conn, GAME)) == {"opponent"}
    assert _status(conn, "opponent") == "changed"


def test_a_venue_alias_counts_as_the_feed_catching_up(conn, synced):
    """The feed still types "Thistledown Park". Once somebody teaches the venue
    table that it is Kingsmere, the feed has said Kingsmere all along."""
    _override(conn, GAME, "location", {"venue_name": "Kingsmere"})
    _sync(conn, synced)
    assert "location" in repo.overrides_of(conn, GAME)

    venue = repo.upsert_venue(conn, name="Kingsmere", address="9 Wren Rd")
    repo.add_venue_alias(conn, venue, "Thistledown Park")
    _sync(conn, synced)

    assert repo.overrides_of(conn, GAME) == {}
    cleared = repo.override_history(conn, GAME)[-1]
    assert cleared["action"] == "auto_clear"
    # The text the feed actually typed, so the clear can be explained.
    assert "Thistledown Park" in json.loads(cleared["upstream"])["raw"]


def test_a_dry_run_judges_nothing(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    caught_up = _feed_with(GAME_START + "\nDTEND:20260829T150000Z",
                           "DTSTART:20260829T150000Z\nDTEND:20260829T160000Z")
    sync_source(conn, repo.get_source(conn, SOURCE), synced, now=NOW,
                raw=caught_up, dry_run=True)
    assert "time" in repo.overrides_of(conn, GAME)


def test_a_poll_never_touches_a_fix_edited_since_it_read_it(conn, synced):
    _override(conn, GAME, "opponent", {"opponent": "Harbour FC Blue"})
    stale = repo.overrides_of(conn, GAME)["opponent"]
    _override(conn, GAME, "opponent", {"opponent": "Harbour FC Green"})

    assert not repo.clear_converged(conn, stale, upstream={"opponent": "x"}, raw=None)
    assert not repo.mark_changed(conn, stale, upstream={"opponent": "x"}, at="now")
    assert repo.overrides_of(conn, GAME)["opponent"].value == {"opponent": "Harbour FC Green"}


def test_a_played_game_is_not_asked_about(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    elsewhere = _feed_with(GAME_START, "DTSTART:20260829T143000Z")
    after = datetime(2026, 8, 30, tzinfo=timezone.utc)
    assert _sync(conn, synced, raw=elsewhere, now=after).fixes_changed == 0
    assert _status(conn) == "active"


# --- the event goes away -------------------------------------------------------


def _without_game() -> bytes:
    text = FIXTURE.read_text()
    blocks = text.split("BEGIN:VEVENT")
    kept = [b for b in blocks if f"UID:{GAME}" not in b]
    return "BEGIN:VEVENT".join(kept).encode()


def test_a_vanished_event_orphans_its_fix_and_a_return_restores_it(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)

    report = _sync(conn, synced, raw=_without_game())
    assert report.status == "ok", report.line()
    assert report.fixes_orphaned == 1
    assert _status(conn) == "orphaned"
    assert GAME not in synced.calendar

    _sync(conn, synced)
    assert _status(conn) == "active"
    assert synced.calendar[GAME].starts_at.isoformat() == MOVED["starts_at"]
    assert "returned" in _actions(conn)


def test_a_held_poll_orphans_nothing(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    text = FIXTURE.read_text()
    blocks = text.split("BEGIN:VEVENT")
    truncated = ("BEGIN:VEVENT".join(blocks[:2]).rstrip() + "\nEND:VCALENDAR\n").encode()
    assert _sync(conn, synced, raw=truncated).status == "held"
    assert _status(conn) == "active"


def test_a_feed_cancellation_is_not_an_orphan(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    _sync(conn, synced, raw=_feed_with(GAME_START, GAME_START + "\nSTATUS:CANCELLED"))
    assert _status(conn) == "active"


def test_an_orphaned_fix_moves_to_the_event_a_person_picks(conn, synced):
    """A coach deleting a game and adding it again mints a new id. calsync
    never guesses the match; a person picks it, and the fix goes there."""
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    readded = FIXTURE.read_text().replace(f"UID:{GAME}", "UID:360Player-event-9999999")
    _sync(conn, synced, raw=readded.encode())
    assert _status(conn) == "orphaned"

    moved = repo.reattach_overrides(conn, GAME, "360Player-event-9999999", actor="dan")
    conn.commit()
    assert moved == ["time"]
    _sync(conn, synced, raw=readded.encode())
    assert synced.calendar["360Player-event-9999999"].starts_at.isoformat() == MOVED["starts_at"]
    assert repo.overrides_of(conn, GAME) == {}


def test_an_orphaned_fix_can_be_forgotten(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    _sync(conn, synced, raw=_without_game())
    assert repo.forget_overrides(conn, GAME, actor="dan") == 1
    assert repo.overrides_of(conn, GAME) == {}
    assert _actions(conn)[-1] == "forgot"


def test_a_fix_cannot_move_to_another_teams_event(conn, synced):
    """The candidates are one team's, but the form posts an id — and a fix
    for Jesse's game must never land on somebody else's."""
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order) VALUES ('robin', 'Robin', 'R', 2);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('robin-swim', 'robin', 'Swim', 'soccer', 'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('robin-feed', 'robin-swim', 'player360', 'feed');
        INSERT INTO event_state (uid, source_id, collection, content_hash, starts_at,
                                 shown_starts_at)
             VALUES ('robin-event', 'robin-feed', 'practices', 'h',
                     '2026-08-30T14:00:00+00:00', '2026-08-30T14:00:00+00:00');
        """
    )
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    _sync(conn, synced, raw=_without_game())

    with pytest.raises(OverrideError, match="another team"):
        repo.reattach_overrides(conn, GAME, "robin-event", actor="dan")
    assert _status(conn) == "orphaned"


def test_a_rescheduled_game_is_asked_about_until_it_is_played(conn, synced):
    """Judged by the start the calendar shows. The feed's date passing means
    nothing when a person moved the game to next week."""
    _override(conn, GAME, "time", {"starts_at": "2026-09-02T14:00:00+00:00",
                                   "ends_at": "2026-09-02T15:00:00+00:00"})
    _sync(conn, synced)
    elsewhere = _feed_with(GAME_START, "DTSTART:20260829T143000Z")
    between = datetime(2026, 8, 30, tzinfo=timezone.utc)
    assert _sync(conn, synced, raw=elsewhere, now=between).fixes_changed == 1


def test_a_moved_events_fix_outlives_the_feeds_date(conn, synced):
    """Pruned by both starts: the fix is what keeps the event on the day it is
    actually on, and it must not age out while that day is still to come."""
    _override(conn, GAME, "time", {"starts_at": "2026-09-20T14:00:00+00:00",
                                   "ends_at": "2026-09-20T15:00:00+00:00"})
    _sync(conn, synced)
    _sync(conn, synced, now=datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert "time" in repo.overrides_of(conn, GAME)
    assert repo.stored_event(conn, GAME) is not None


def test_a_line_break_in_the_notes_is_a_change():
    a = {"body": "Bring water. Bring chairs."}
    b = {"body": "Bring water.\nBring chairs."}
    assert not overlay.same("notes", a, b)
    assert overlay.same("notes", {"body": "Bring water.  \n"}, {"body": "Bring water."})
    # Elsewhere a coach's doubled space is still nothing.
    assert overlay.same("opponent", {"opponent": "Harbour  FC"}, {"opponent": "Harbour FC"})


def test_the_api_stops_crediting_a_person_the_moment_a_fix_is_lifted(conn, synced):
    _override(conn, GAME, "time", MOVED)
    _sync(conn, synced)
    caught_up = _feed_with(GAME_START + "\nDTEND:20260829T150000Z",
                           "DTSTART:20260829T150000Z\nDTEND:20260829T160000Z")
    _sync(conn, synced, raw=caught_up)

    item = repo.stored_event(conn, GAME)
    assert item.overrides is None
    assert api_app._resolution(conn, item)["starts_at"]["owner"] == "source"


def test_a_time_typed_across_the_november_change_is_the_local_time(conn, synced):
    """The game is in August, daylight time. Moved into November, 10:00 is
    10:00 standard time — the zone's rule for that date, not the event's old
    offset carried across."""
    from calsync.web import fix as fix_mod

    subject = fix_mod.subject(conn, GAME)
    plan = fix_mod.plan(subject, {"date": "2026-11-07", "start": "10:00", "end": "11:00"})
    value = plan.sets["time"]
    assert value["starts_at"] == "2026-11-07T10:00:00-05:00"
    assert plan.moved_day is not None

    repo.set_override(conn, GAME, "time", value, reason="moved")
    conn.commit()
    _sync(conn, synced)
    assert synced.calendar[GAME].starts_at == datetime(2026, 11, 7, 15, tzinfo=timezone.utc)
    assert "Start 10:00 EST" in synced.calendar[GAME].body


def test_a_fix_lifted_through_an_alias_rewrites_the_calendar_once(conn, synced):
    """An alias match is the same *place*, not the same text: the calendar
    still shows the fix's spelling and address. Lifting it has to let the next
    poll write the feed's version, or the calendar and the API disagree with
    no further change ever coming to settle it."""
    _override(conn, GAME, "location", {"venue_name": "Kingsmere"})
    _sync(conn, synced)
    assert synced.calendar[GAME].location_text == "Kingsmere"

    venue = repo.upsert_venue(conn, name="Kingsmere", address="9 Wren Rd")
    repo.add_venue_alias(conn, venue, "Thistledown Park")
    _sync(conn, synced)
    assert repo.overrides_of(conn, GAME) == {}
    synced.take()

    _sync(conn, synced)
    assert GAME in synced.take()
    assert synced.calendar[GAME].location_text == "Kingsmere, 9 Wren Rd"
    assert repo.stored_event(conn, GAME).event.venue.address == "9 Wren Rd"
    _sync(conn, synced)
    assert synced.take() == []
