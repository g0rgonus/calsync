"""calsync remembers what an event was, not just where it was put.

`event_state` answers "did this change, and where did it go". These tests are
about the other half — enough of the event to answer "what is on at five on
Thursday" without going back to the feed, which is what `docs/API.md`'s read
endpoints could not previously be built on.

The property under test throughout is that the stored copy **cannot disagree
with the calendar**. It is written behind the same barrier as the placement
record — after the target has accepted the write — so a refused write leaves
neither, and a divergence discovered later is repaired by re-writing both.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from calsync import db, repo
from calsync.settings import Settings
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
                                league, age_group, tz)
             VALUES ('jesse-soccer-vanguard', 'jesse', 'Vanguard', 'soccer', 'U10PL',
                     'PSL', 'U10', 'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('p360-jesse-vanguard', 'jesse-soccer-vanguard', 'player360', 'feed');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def source(conn):
    return repo.list_sources(conn)[0]


@pytest.fixture
def target(tmp_path):
    return build("ics_file", directory=tmp_path / "out")


def _sync(conn, source, target, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("raw", FIXTURE.read_bytes())
    return sync_source(conn, source, target, **kwargs)


def _stored(conn, source_id="p360-jesse-vanguard"):
    return repo.event_contents(conn, source_id)


# --- the question the feed used to be the only answer to --------------------


def test_a_synced_event_can_be_described_without_the_feed(conn, source, target):
    """The point of the whole table: content, not just a hash and a collection."""
    _sync(conn, source, target)

    fixture = next(
        content for content in _stored(conn).values() if content["opponent"]
    )
    # As the adapter normalized it — "U10" identifies the age group, not them.
    assert fixture["opponent"] == "Harbour FC"
    assert fixture["is_game"] == 1
    assert fixture["venue_name"] == "Thistledown Park"
    assert fixture["venue_address"] == "1009 Thistledown Rd, Marbury NX 40114"
    assert fixture["ends_at"].startswith("2026-")
    assert fixture["tz"] == "America/New_York"


def test_no_rendered_title_is_stored_anywhere(conn, source, target):
    """The title is a render. Storing one is what stops it from staying one."""
    _sync(conn, source, target)

    assert "summary" not in repo.CONTENT_COLUMNS
    assert "title" not in repo.CONTENT_COLUMNS

    # Belt and braces: no stored value is the string the calendar shows, so a
    # naming-convention change cannot leave a stale copy behind.
    written = (Path(target.directory) / "games").rglob("*.ics")
    titles = {
        line.split(":", 1)[1].strip()
        for path in written
        for line in path.read_text().splitlines()
        if line.startswith("SUMMARY:")
    }
    assert titles, "fixture produced no games, so this proves nothing"
    stored_values = {
        str(value)
        for content in _stored(conn).values()
        for value in content.values()
        if value is not None
    }
    assert not (titles & stored_values)


def test_home_is_never_flattened_to_a_guess(conn, source, target):
    """Tri-state has to survive the round trip.

    Player360 phrases every fixture as "vs", so an unknown side collapsed to 0
    on the way into SQLite would come back out as a positive claim that the
    fixture is away.
    """
    _sync(conn, source, target)

    assert any(content["home"] is None for content in _stored(conn).values())


# --- the stored copy cannot outrun the calendar -----------------------------


class _RefusingTarget:
    """Accepts collections, refuses every write."""

    def __init__(self):
        self.directory = None

    def ensure_collection(self, collection):
        pass

    def upsert(self, event, previous=None):
        raise TargetError("the calendar said no")

    def cancel(self, ref):
        raise TargetError("the calendar said no")


def test_a_refused_write_stores_no_content(conn, source):
    """A copy the calendar never got would be the one new failure mode."""
    report = sync_source(
        conn, source, _RefusingTarget(), now=NOW, raw=FIXTURE.read_bytes()
    )

    assert report.status == "error"
    assert _stored(conn) == {}


def test_a_dry_run_stores_nothing(conn, source, target):
    """The console runs one of these on every page load."""
    report = _sync(conn, source, target, dry_run=True)

    assert report.created > 0
    assert _stored(conn) == {}


# --- content is checked independently of the feed's hash --------------------


def test_a_stable_feed_refreshes_nothing(conn, source, target):
    """The comparison must converge, or every poll re-writes the season."""
    _sync(conn, source, target)
    second = _sync(conn, source, target)

    assert second.refreshed == 0
    assert second.updated == 0
    assert second.created == 0


def test_events_written_before_content_existed_backfill_themselves(
    conn, source, target
):
    """Upgrading calsync must not leave the read side empty until a feed changes.

    A stable season's hashes never change, so nothing would ever re-write these
    events. Missing content has to read as a difference like any other.
    """
    first = _sync(conn, source, target)
    conn.execute("DELETE FROM event_content")
    conn.commit()

    second = _sync(conn, source, target)

    assert second.refreshed == first.created
    assert second.updated == 0, "a backfill is not a feed change"
    assert second.created == 0
    assert len(_stored(conn)) == first.created


def test_teaching_a_venue_reaches_the_calendar(conn, source, target, tmp_path):
    """The gap this closes, stated as the behaviour somebody actually wanted.

    The diff hashes the *raw feed component*, before the venue table is
    consulted — so renaming a venue changed what an event should say while
    leaving its hash identical, and the correction never reached anyone's phone.
    """
    _sync(conn, source, target)

    venue_id = repo.upsert_venue(
        conn, name="Thistledown Regional Park",
        address="1009 Thistledown Rd, Marbury, NX 40114",
    )
    repo.add_venue_alias(conn, venue_id, "Thistledown Park")

    report = _sync(conn, source, target)

    assert report.refreshed > 0
    locations = {
        line.split(":", 1)[1].strip()
        for path in (tmp_path / "out").rglob("*.ics")
        for line in path.read_text().splitlines()
        if line.startswith("LOCATION:")
    }
    assert any("Thistledown Regional Park" in text for text in locations)
    assert all(
        content["venue_name"] != "Thistledown Park"
        for content in _stored(conn).values()
        if content["venue_raw"] and "Thistledown" in content["venue_raw"]
    )


def test_editing_a_team_reaches_the_calendar(conn, source, target, tmp_path):
    """The console's team form is not cosmetic — it changes how the feed parses.

    `age_group` feeds `Activity.known_tokens`, which is what decides whether
    "U10PL PSL Match vs Harbour FC U10" yields an opponent or a mangled one. The
    hash is over the unchanged feed, so before content was compared this edit
    changed the parse and nothing else: the correction sat in the database and
    never reached the family's calendar.
    """
    def edit(age_group):
        repo.update_activity(
            conn, "jesse-soccer-vanguard", name="Vanguard", emoji=None,
            official_name="U10PL", short_name=None, league="PSL",
            age_group=age_group, home_venue_id=None,
            alarm_game_min=90, alarm_practice_min=30,
        )

    edit(None)  # the state somebody onboarding a team in a hurry leaves it in
    _sync(conn, source, target)
    uid = next(uid for uid, c in _stored(conn).items() if c["opponent"])
    before = _summary(tmp_path, uid)

    edit("U10")
    report = _sync(conn, source, target)

    assert report.refreshed == 1
    assert before.endswith("Harbour FC U10")
    assert _summary(tmp_path, uid).endswith("Harbour FC")


def _summary(tmp_path, uid):
    path = next((tmp_path / "out").rglob(f"{uid}.ics"))
    return next(
        line for line in path.read_text().splitlines() if line.startswith("SUMMARY:")
    )


def test_a_refresh_is_not_reported_as_a_feed_change(conn, source, target):
    """`updated` means the feed moved something. Refreshes must not inflate it."""
    _sync(conn, source, target)
    conn.execute("DELETE FROM event_content")
    conn.commit()

    report = _sync(conn, source, target)

    assert "refreshed" in report.line()
    assert "0 changed" in report.line()


# --- retention --------------------------------------------------------------


def test_content_ages_out_but_the_record_of_writing_it_does_not(conn, source, target):
    """Children's names and locations are kept as briefly as they are useful.

    The `event_state` row survives the prune: it is what recognises the event if
    it ever comes back, and it holds nothing but a uid, a hash and a timestamp.
    """
    _sync(conn, source, target)
    before_prune = len(_stored(conn))
    assert before_prune > 0

    removed = repo.prune_event_content(conn, before="2099-01-01T00:00:00+00:00")
    conn.commit()

    assert removed == before_prune
    assert _stored(conn) == {}
    assert repo.tracked_events(conn, source.id) == before_prune


def test_a_poll_prunes_content_outside_the_sync_window(conn, source, target):
    """Self-maintaining, so nothing has to remember to run a cleanup."""
    _sync(conn, source, target)
    assert _stored(conn)

    settings = Settings.load(conn)
    # Far enough past the fixture that every event has aged out of the window
    # behind us — which is also the bound the diff compares across, so this is
    # the same set of events the loop has already stopped considering.
    later = NOW + timedelta(days=365 + settings.sync_window_back_days)
    _sync(conn, source, target, now=later)

    assert _stored(conn) == {}


def test_a_cancelled_event_keeps_its_content(conn, source, target):
    """A tombstone still has to be describable.

    `docs/API.md` makes cancellation a tombstone rather than a purge precisely so
    the deletion propagates; a tombstone nobody can render is not much of one.
    """
    _sync(conn, source, target)
    uid = next(iter(_stored(conn)))
    repo.mark_event_cancelled(conn, uid)
    conn.commit()

    assert uid in _stored(conn)


def test_deleting_a_child_takes_the_content_with_it(conn, source, target):
    """The cascade already reaches `event_state`; it must not stop short here.

    Deleting a child is refused while anything depends on them (`child_usage`),
    but when it does happen it has to be a real deletion — a stranded copy of a
    kid's schedule is the worst possible remnant.
    """
    _sync(conn, source, target)
    assert _stored(conn)

    repo.delete_child(conn, "jesse")

    assert _stored(conn) == {}


def test_an_all_day_event_round_trips_through_the_receipt(conn, target):
    """The receipt has to remember it. A re-read that lost the flag would
    render the semifinal at midnight, which is the bug this exists to avoid."""
    from calsync import repo as _repo

    conn.executescript(
        """
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('jesse-soccer-kestrels', 'jesse', 'Kestrels', 'soccer',
                     'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('tr-kestrels', 'jesse-soccer-kestrels', 'teamreach', 'feed');
        """
    )
    conn.commit()
    raw = (Path(__file__).parent / "fixtures" / "teamreach_allday_sample.ics").read_bytes()
    source = _repo.get_source(conn, "tr-kestrels")
    sync_source(conn, source, target,
                now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc), raw=raw)

    stored = {
        item.event.uid: item.event
        for item in _repo.stored_events(conn, start="2026-01-01", end="2027-06-01")
    }
    assert stored["31000002@teamreach"].all_day is True
    assert stored["31000001@teamreach"].all_day is False


def test_all_day_is_part_of_the_stored_content(conn):
    """So a feed that switches an event from a day to a time is a change the
    sync loop sees, rather than one it reads past."""
    from calsync import repo as _repo

    assert "all_day" in _repo.CONTENT_COLUMNS
