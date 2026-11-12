"""A club calendar, routed by category.

The fixture keeps the shape of a Google public export: a VTIMEZONE, a weekly
series with WKST=SU and two RECURRENCE-ID overrides, a monthly BYDAY=1WE series
with no end, an all-day VALUE=DATE event, and events with and without LOCATION
and with no DESCRIPTION. What each test pins is in
docs/connectors/calendar-feed.md.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from calsync import categories, db, onboarding, repo, routing, sources
from calsync.models import Activity
from calsync.secrets import SecretStore
from calsync.sources.calendar_feed import FeedError
from calsync.sync import SyncReport, sync_source
from calsync.targets import build

FIXTURE = Path(__file__).parent / "fixtures" / "calendar_feed_sample.ics"
NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
NY = ZoneInfo("America/New_York")
SERIES = "0a1b2c3d4e5f6g7h8i9j0k1l2m@google.com"
MONTHLY = "5n6o7p8q9r0s1t2u3v4w5x6y7z@google.com"


class Pushover:
    def __init__(self):
        self.sent = []

    def __call__(self, _config, _secrets, message, *, title="", url=None, url_title=None):
        self.sent.append({"title": title, "message": message, "url": url})


class Store:
    def get(self, ref):
        return {"pushover_token": "t", "pushover_user": "u"}[ref]

    def has(self, ref):
        return True


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('luke', 'Luke', 'Lu', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('luke-scouts-explorers', 'luke', 'Explorers', 'scouts',
                     'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape, config)
             VALUES ('cal-explorers', 'luke-scouts-explorers', 'calendar-feed', 'feed',
                     '{"default_calendar": "explorers", "categories": {}}');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def target(tmp_path):
    return build("ics_file", directory=tmp_path / "out")


def _configure(conn, **changes):
    source = repo.get_source(conn, "cal-explorers")
    config = dict(source.config or {})
    config.update(changes)
    conn.execute("UPDATE sources SET config = ? WHERE id = 'cal-explorers'",
                 (json.dumps(config),))
    conn.commit()


def _sync(conn, target, raw=None, **kwargs):
    source = repo.get_source(conn, "cal-explorers")
    return sync_source(conn, source, target, now=kwargs.pop("now", NOW),
                       raw=raw if raw is not None else FIXTURE.read_bytes(),
                       publish=False, **kwargs)


def _parse(raw=None, window=None):
    act = Activity(id="a", child_id="luke", name="Explorers", sport="scouts",
                   emoji="⚜️", tz="America/New_York")
    return sources.parse("calendar-feed", raw if raw is not None else FIXTURE.read_bytes(),
                         act, source_id="cal-explorers",
                         window=window or (NOW - timedelta(days=30), NOW + timedelta(days=180)))


def _live(conn):
    return {uid: s for uid, s in repo.event_states(conn, "cal-explorers").items()
            if not s.cancelled}


def _by_category(result, name):
    return [e for e in result.events if e.source_category == name]


# --- routing ----------------------------------------------------------------


def test_new_category_waits_on_staging_before_promotion(conn, target):
    repo.set_staging(conn, "cal-explorers", "onboarding")
    report = _sync(conn, target)

    assert report.created > 0
    assert {s.collection for s in _live(conn).values()} == {"onboarding"}


def test_new_category_goes_to_default_after_promotion(conn, target):
    _configure(conn, categories={"Padawan Meeting": "padawans"})
    _sync(conn, target)

    collections = {s.collection for uid, s in _live(conn).items()
                   if not uid.startswith(SERIES)}
    assert collections == {"explorers"}, "an unset category goes to the default"
    assert {s.collection for uid, s in _live(conn).items()
            if uid.startswith(SERIES)} == {"padawans"}


def test_new_categories_do_not_block_promotion(conn, target):
    no_places = re.sub(rb"(?m)^LOCATION:[^\n]*\n", b"", FIXTURE.read_bytes())
    repo.set_staging(conn, "cal-explorers", "onboarding")
    report = _sync(conn, target, raw=no_places, dry_run=True)

    assert report.categories, "every category is new on the first poll"
    assert report.is_clean
    assert report.promotable


def test_promotable_on_events_without_games(conn, target):
    no_places = re.sub(rb"(?m)^LOCATION:[^\n]*\n", b"", FIXTURE.read_bytes())
    report = _sync(conn, target, raw=no_places, dry_run=True)

    assert report.fixtures_seen == 0
    assert report.events_seen > 0
    assert report.promotable


def test_sports_feed_still_needs_a_fixture_to_promote():
    practices_only = SyncReport(source_id="tr-rogues", events_seen=12, fixtures_seen=0)
    assert not practices_only.promotable
    assert SyncReport(source_id="cal", events_seen=12, events_only=True).promotable


def test_skipped_category_never_written(conn, target, tmp_path):
    _configure(conn, categories={"Council Meeting": "skip"})
    report = _sync(conn, target)

    assert not [uid for uid in repo.event_states(conn, "cal-explorers")
                if uid.startswith(MONTHLY)]
    assert report.skipped > 0
    assert report.withheld == 0, "a skipped category is not a withheld event"
    assert not [p for p in (tmp_path / "out").rglob("*.ics") if MONTHLY in p.name]


def test_skipping_a_written_category_removes_without_tripping_guard(conn, target):
    _sync(conn, target)
    written = [uid for uid in _live(conn) if uid.startswith(SERIES)]
    assert len(written) > 3, "enough to trip the guard if they counted"

    _configure(conn, categories={"Padawan Meeting": "skip"})
    report = _sync(conn, target)

    assert report.status == "ok", report.held
    assert not report.held
    assert not [uid for uid in _live(conn) if uid.startswith(SERIES)]
    rows = conn.execute("SELECT withheld FROM event_state WHERE uid LIKE ?",
                        (SERIES + "%",)).fetchall()
    assert all(r["withheld"] is None for r in rows), "never stored as withheld"

    _configure(conn, categories={})
    _sync(conn, target)
    assert len([uid for uid in _live(conn) if uid.startswith(SERIES)]) == len(written)


def test_moving_a_category_moves_its_events(conn, target):
    _sync(conn, target)
    _configure(conn, categories={"council meeting": "Council"})
    report = _sync(conn, target)

    council = {uid: s for uid, s in _live(conn).items() if uid.startswith(MONTHLY)}
    assert council and {s.collection for s in council.values()} == {"council"}
    assert report.moved == len(council)
    assert report.created == 0 and report.cancelled == 0


# --- title rules ------------------------------------------------------------

MEETINGS = {"match": "contains", "text": "meeting", "destination": "meetings"}


def _collections(conn, prefix):
    return {s.collection for uid, s in _live(conn).items() if uid.startswith(prefix)}


def test_rule_routes_matching_categories(conn, target):
    _configure(conn, rules=[MEETINGS])
    _sync(conn, target)

    assert _collections(conn, SERIES) == {"meetings"}
    assert _collections(conn, MONTHLY) == {"meetings"}
    assert routing.placement("Group  Meeting", repo.get_source(conn, "cal-explorers").config) \
        == ("meetings", "rule", 0), "ignores case and spacing like a category"
    assert routing.placement("Mos Espa Camporee", repo.get_source(conn, "cal-explorers").config) \
        == ("explorers", "default", None)


def test_first_matching_rule_wins(conn, target):
    _configure(conn, rules=[{"match": "starts_with", "text": "council", "destination": "council"},
                            MEETINGS])
    _sync(conn, target)

    assert _collections(conn, MONTHLY) == {"council"}
    assert _collections(conn, SERIES) == {"meetings"}


def test_category_destination_beats_rule(conn, target):
    _configure(conn, rules=[MEETINGS], categories={"Council Meeting": "council"})
    _sync(conn, target)

    assert _collections(conn, MONTHLY) == {"council"}
    assert _collections(conn, SERIES) == {"meetings"}


def test_rule_placed_category_is_not_new():
    config = {"default_calendar": "explorers", "rules": [MEETINGS]}
    assert not routing.is_new("Den Meeting - Special", config)
    assert routing.is_new("Mos Espa Camporee", config)


def test_rule_placed_category_is_not_new_and_sends_no_push(conn, target):
    _configure(conn, rules=[MEETINGS],
               categories={"Mos Espa Camporee": "explorers", "Elders' Roundtable": "skip"})
    pushover = Pushover()
    report = _sync(conn, target)
    outcome = categories.review(conn, repo.get_source(conn, "cal-explorers"), report,
                                secrets=Store(), sender=pushover)

    assert outcome.new == ()
    assert pushover.sent == []


def test_skip_rule_removes_written_events_without_tripping_guard(conn, target):
    _sync(conn, target)
    assert len([uid for uid in _live(conn) if uid.startswith(SERIES)]) > 3

    _configure(conn, rules=[{"match": "starts_with", "text": "Padawan", "destination": "skip"}])
    report = _sync(conn, target)

    assert report.status == "ok" and not report.held
    assert not [uid for uid in _live(conn) if uid.startswith(SERIES)]
    rows = conn.execute("SELECT withheld FROM event_state WHERE uid LIKE ?",
                        (SERIES + "%",)).fetchall()
    assert all(r["withheld"] is None for r in rows), "never stored as withheld"


def test_hold_beats_category_and_rule(conn, target):
    _configure(conn, rules=[MEETINGS])
    _sync(conn, target, raw=_with_slip())
    assert _live(conn)["slip@google.com"].collection == "enrichment"

    _configure(conn, categories={"Padawan Meeting": "padawans"})
    _sync(conn, target, raw=_with_slip())
    assert _live(conn)["slip@google.com"].collection == "enrichment"
    assert _collections(conn, SERIES) == {"padawans"}


def test_changing_a_rule_moves_written_events(conn, target):
    _configure(conn, rules=[MEETINGS])
    _sync(conn, target)
    _configure(conn, rules=[{**MEETINGS, "destination": "gatherings"}])
    report = _sync(conn, target)

    assert _collections(conn, MONTHLY) == {"gatherings"}
    assert report.moved > 0
    assert report.created == 0 and report.cancelled == 0


def test_category_in_categories_seen_stays_not_new_when_a_rule_drops_it():
    kept = {"default_calendar": "explorers", "categories_seen": ["Council  meeting"]}
    assert not routing.is_new("Council Meeting", {**kept, "rules": [MEETINGS]})
    assert not routing.is_new("Council Meeting", kept), "kept at the default already"
    assert routing.is_new("Group Meeting", kept)


# --- default places ---------------------------------------------------------

GROUP = "1n2o3p4q5r6s7t8u9v0w1x2y3z@google.com"


def _hall(conn, name="Naboo Scout Hall"):
    venue_id = repo.upsert_venue(conn, name=name, address="12 Palace Way, Theed NX 40110",
                                 lat=40.1, lon=-75.2, pin_confirmed=True)
    conn.commit()
    return venue_id


def _venue_of(conn, uid):
    return repo.event_contents(conn, "cal-explorers")[uid]["venue_name"]


def test_default_place_fills_missing_location(conn, target, tmp_path):
    _configure(conn, places={"group meeting": _hall(conn)})
    report = _sync(conn, target)

    assert _venue_of(conn, GROUP) == "Naboo Scout Hall", "recorded as what the source said"
    assert "unresolved_venues" not in report.diagnostics or \
        "Naboo Scout Hall" not in report.diagnostics["unresolved_venues"]
    (written,) = [p for p in (tmp_path / "out").rglob("*.ics") if GROUP in p.name]
    assert "Naboo Scout Hall" in written.read_text()


def test_feed_location_beats_default_place(conn, target):
    _configure(conn, places={"Council Meeting": _hall(conn)})
    _sync(conn, target)

    assert _venue_of(conn, next(u for u in _live(conn) if u.startswith(MONTHLY))) \
        == "Lars Homestead Elementary School"


def test_rule_place_fills_and_category_place_beats_it(conn, target):
    hall, den = _hall(conn), _hall(conn, "Mos Espa Den")
    _configure(conn, rules=[{**MEETINGS, "place": hall}])
    _sync(conn, target)
    assert _venue_of(conn, GROUP) == "Naboo Scout Hall"

    _configure(conn, places={"Group Meeting": den})
    _sync(conn, target)
    assert _venue_of(conn, GROUP) == "Mos Espa Den"


def test_merging_a_venue_moves_default_places_and_deleting_drops_them(conn, target):
    hall, den = _hall(conn), _hall(conn, "Mos Espa Den")
    _configure(conn, places={"Group Meeting": hall, "Council Meeting": den},
               rules=[{**MEETINGS, "place": hall}, {**MEETINGS, "place": den}])

    repo.merge_venues(conn, losing_id=hall, winning_id=den)
    config = repo.get_source(conn, "cal-explorers").config
    assert config["places"] == {"Group Meeting": den, "Council Meeting": den}
    assert [r["place"] for r in config["rules"]] == [den, den]

    repo.delete_venue(conn, den)
    config = repo.get_source(conn, "cal-explorers").config
    assert config["places"] == {}
    assert all("place" not in r for r in config["rules"])
    _sync(conn, target)
    assert _venue_of(conn, GROUP) is None


def test_setting_a_default_place_refreshes_without_a_hash_change(conn, target):
    _sync(conn, target)
    before = repo.event_states(conn, "cal-explorers")[GROUP]
    assert _venue_of(conn, GROUP) is None

    _configure(conn, places={"Group Meeting": _hall(conn)})
    report = _sync(conn, target)

    after = repo.event_states(conn, "cal-explorers")[GROUP]
    assert after.content_hash == before.content_hash
    assert report.refreshed == 1
    assert report.updated == 0 and report.created == 0
    assert _venue_of(conn, GROUP) == "Naboo Scout Hall"


# --- the feed ---------------------------------------------------------------


def test_cancelled_status_is_honoured_and_not_hashed(conn, target):
    group = "1n2o3p4q5r6s7t8u9v0w1x2y3z@google.com"
    raw = FIXTURE.read_bytes()
    off = raw.replace(b"SUMMARY:Group  Meeting", b"SUMMARY:Group  Meeting\nSTATUS:CANCELLED")
    off = off.replace(b"STATUS:CONFIRMED\nSUMMARY:Group  Meeting", b"SUMMARY:Group  Meeting")

    before = next(e for e in _parse(raw).events if e.uid == group)
    after = next(e for e in _parse(off).events if e.uid == group)
    assert after.cancelled and not before.cancelled
    assert after.content_hash == before.content_hash

    _sync(conn, target)
    report = _sync(conn, target, raw=off)
    assert report.called_off == 1
    assert group not in _live(conn)


def test_recurrence_override_keeps_occurrence_uid():
    events = {e.uid: e for e in _parse().events}

    moved = events[f"{SERIES}/20261013T230000Z"]
    assert moved.starts_at.astimezone(NY) == datetime(2026, 10, 14, 19, tzinfo=NY)
    assert f"{SERIES}/20261014T230000Z" not in events
    relocated = events[f"{SERIES}/20261111T000000Z"]
    assert relocated.venue.name == "Jedi Temple Memorial Park"


def test_recurrence_keeps_wall_clock_across_dst():
    meetings = [e for e in _parse().events if e.uid.startswith(SERIES)]
    before = [e for e in meetings if e.starts_at < datetime(2026, 11, 1, tzinfo=NY)]
    after = [e for e in meetings if e.starts_at > datetime(2026, 11, 2, tzinfo=NY)]

    assert before and after
    for e in meetings:
        assert (e.starts_at.astimezone(NY).hour, e.starts_at.astimezone(NY).minute) == (19, 0)
    # The instant moves by the hour the clocks did.
    assert {e.starts_at.astimezone(timezone.utc).hour for e in before} == {23}
    assert {e.starts_at.astimezone(timezone.utc).hour for e in after} == {0}


def test_unbounded_series_stops_at_window():
    window = (NOW, NOW + timedelta(days=120))
    council = [e for e in _parse(window=window).events if e.uid.startswith(MONTHLY)]

    assert len(council) == 3    # the first Wednesdays of Oct, Nov and Dec
    assert all(window[0] <= e.starts_at <= window[1] for e in council)
    assert all(e.starts_at.astimezone(NY).weekday() == 2
               and e.starts_at.astimezone(NY).day <= 7 for e in council)


def test_refused_rule_part_raises():
    raw = FIXTURE.read_bytes().replace(b"RRULE:FREQ=MONTHLY;BYDAY=1WE",
                                       b"RRULE:FREQ=MONTHLY;BYDAY=WE;BYSETPOS=1")
    with pytest.raises(FeedError, match=rf"{re.escape(MONTHLY)}.*BYSETPOS"):
        _parse(raw)


def test_exdate_in_utc_removes_a_local_occurrence():
    raw = FIXTURE.read_bytes().replace(
        b"RRULE:FREQ=WEEKLY;WKST=SU;UNTIL=20270526T035959Z;BYDAY=TU\n",
        b"RRULE:FREQ=WEEKLY;WKST=SU;UNTIL=20270526T035959Z;BYDAY=TU\n"
        b"EXDATE:20260922T230000Z\n")
    uids = {e.uid for e in _parse(raw).events}

    assert f"{SERIES}/20260922T230000Z" not in uids
    assert f"{SERIES}/20260929T230000Z" in uids


def test_override_without_its_series_keeps_the_occurrence_uid():
    whole = {e.uid: e for e in _parse().events}
    raw = FIXTURE.read_bytes()
    master_start = raw.index(b"BEGIN:VEVENT")
    master_end = raw.index(b"END:VEVENT", master_start) + len(b"END:VEVENT\n")
    alone = {e.uid: e for e in _parse(raw[:master_start] + raw[master_end:]).events}

    moved = f"{SERIES}/20261013T230000Z"
    assert moved in alone, "keyed by RECURRENCE-ID, not by its new start"
    assert alone[moved].starts_at == whole[moved].starts_at


def test_events_only_card_waits_for_events():
    from calsync.web import gate

    report = SyncReport(source_id="cal", events_only=True)
    activity = Activity(id="a", child_id="luke", name="Explorers", sport="scouts",
                        emoji="⚜️", tz="America/New_York")
    assert gate.summarise(gate.conditions(report, activity)) == \
        ("waiting", "waiting for events")


def test_categories_count_only_the_sync_window():
    late = (datetime(2026, 11, 2, tzinfo=timezone.utc), datetime(2026, 12, 31, tzinfo=timezone.utc))
    result = _parse(window=late)

    assert "Mos Espa Camporee" not in result.categories, "October only, so past the window"
    assert "Group Meeting" not in result.categories
    assert result.categories["Padawan Meeting"] == len(
        [e for e in result.events if e.source_category == "Padawan Meeting"
         and late[0] <= e.starts_at <= late[1]])


#: A Padawan Meeting typed at 07:00 where the series is always 19:00.
SLIP = b"""BEGIN:VEVENT
DTSTART;TZID=America/New_York:20261124T070000
DTEND;TZID=America/New_York:20261124T083000
UID:slip@google.com
SUMMARY:Padawan Meeting
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20261125T073000
DTEND;TZID=America/New_York:20261125T083000
UID:breakfast@google.com
SUMMARY:Padawan Meeting
END:VEVENT
END:VCALENDAR
"""


def _with_slip():
    return FIXTURE.read_bytes().replace(b"END:VCALENDAR\n", SLIP)


def test_twelve_hours_off_is_held_and_asked(conn, target):
    report = _sync(conn, target, raw=_with_slip())

    assert report.diagnostics["suspect_times"] == [
        "Padawan Meeting, Tue 24 Nov 2026 at 07:00 (usually 19:00)"]
    assert _live(conn)["slip@google.com"].collection == "enrichment"
    assert _live(conn)["breakfast@google.com"].collection == "explorers", \
        "07:30 is another time"
    assert report.awaiting_review == 1
    assert not report.promotable


def test_a_confirmed_time_is_released(conn, target):
    _sync(conn, target, raw=_with_slip())
    _configure(conn, times_confirmed=[
        "Padawan Meeting, Tue 24 Nov 2026 at 07:00 (usually 19:00)"])
    report = _sync(conn, target, raw=_with_slip())

    assert "suspect_times" not in report.diagnostics
    assert _live(conn)["slip@google.com"].collection == "explorers"
    assert report.moved == 1


def test_a_corrected_start_is_not_asked_about(conn, target):
    _sync(conn, target, raw=_with_slip())
    repo.set_override(conn, "slip@google.com", "time", {
        "starts_at": "2026-11-24T19:00:00-05:00", "ends_at": "2026-11-24T20:30:00-05:00",
        "all_day": 0}, reason="the den leader confirmed 7pm", actor="anakin")
    conn.commit()
    report = _sync(conn, target, raw=_with_slip())

    assert "suspect_times" not in report.diagnostics
    assert _live(conn)["slip@google.com"].collection == "explorers"


def test_a_tie_has_no_usual_time(conn, target):
    # Five at 07:00 beside five at 19:00: the series cut to three, plus its
    # two moved occurrences, which keep their 19:00 start.
    raw = FIXTURE.read_bytes().replace(b"UNTIL=20270526T035959Z", b"COUNT=3")
    raw = raw.replace(b"END:VCALENDAR\n", b"".join(
        b"BEGIN:VEVENT\nDTSTART;TZID=America/New_York:2026120%dT070000\n"
        b"DTEND;TZID=America/New_York:2026120%dT080000\nUID:am%d@google.com\n"
        b"SUMMARY:Padawan Meeting\nEND:VEVENT\n" % (d, d, d) for d in (1, 2, 3, 4, 5))
        + b"END:VCALENDAR\n")
    report = _sync(conn, target, raw=raw, dry_run=True)

    assert "suspect_times" not in report.diagnostics


def test_a_skipped_category_is_not_asked_about(conn, target):
    _configure(conn, categories={"Padawan Meeting": "skip"})
    report = _sync(conn, target, raw=_with_slip(), dry_run=True)
    assert "suspect_times" not in report.diagnostics


def test_series_over_the_cap_raises():
    raw = FIXTURE.read_bytes().replace(b"RRULE:FREQ=MONTHLY;BYDAY=1WE", b"RRULE:FREQ=DAILY")
    with pytest.raises(FeedError, match="more than 400"):
        _parse(raw, window=(NOW, NOW + timedelta(days=500)))


def test_categories_ignore_case_and_spacing():
    result = _parse()

    assert "Group Meeting" in result.categories, "doubled space tidied"
    assert result.categories["Mos Espa Camporee"] == 1
    assert result.categories["Padawan Meeting"] == len(_by_category(result, "Padawan Meeting"))


def test_all_day_event_is_anchored_at_local_midnight():
    camp = _by_category(_parse(), "Mos Espa Camporee")[0]

    assert camp.all_day
    assert camp.starts_at == datetime(2026, 10, 17, tzinfo=NY)
    assert camp.ends_at == datetime(2026, 10, 19, tzinfo=NY)


def test_empty_feed_raises():
    with pytest.raises(FeedError):
        _parse(b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")


def test_floating_time_without_calendar_zone_raises():
    raw = FIXTURE.read_bytes().replace(b"X-WR-TIMEZONE:America/New_York\n", b"")
    raw = raw.replace(b"DTSTART;TZID=America/New_York:20261020T183000",
                      b"DTSTART:20261020T183000")
    raw = raw.replace(b"DTEND;TZID=America/New_York:20261020T200000",
                      b"DTEND:20261020T200000")
    with pytest.raises(FeedError, match="floating"):
        _parse(raw)


# --- telling someone --------------------------------------------------------


def test_new_category_pushes_once_with_its_own_copy(conn, target):
    _configure(conn, categories={"Padawan Meeting": "explorers",
                                 "Council Meeting": "explorers",
                                 "Mos Espa Camporee": "explorers",
                                 "Elders' Roundtable": "skip"})
    pushover = Pushover()

    def poll():
        report = _sync(conn, target)
        return categories.review(conn, repo.get_source(conn, "cal-explorers"), report,
                                 secrets=Store(), base_url="http://box:8730",
                                 sender=pushover)

    first = poll()
    assert first.notified == ("Group Meeting",)
    assert pushover.sent == [{
        "title": 'Explorers: new category "Group Meeting"',
        "message": "1 event added to the explorers calendar. "
                   "Keep it there, move it, or skip it.",
        "url": "http://box:8730/sources/cal-explorers#categories",
    }]
    assert "nothing has gone" not in pushover.sent[0]["message"]

    poll()
    assert len(pushover.sent) == 1, "one push per category, never one per poll"
    row = conn.execute("SELECT review_notified FROM sources").fetchone()
    assert row["review_notified"] is None, "the held-events column is untouched"


def test_staged_source_records_categories_without_pushing(conn, target):
    repo.set_staging(conn, "cal-explorers", "onboarding")
    pushover = Pushover()
    report = _sync(conn, target)
    categories.review(conn, repo.get_source(conn, "cal-explorers"), report,
                      secrets=Store(), sender=pushover)

    assert pushover.sent == []
    stored = conn.execute("SELECT category_notified FROM sources").fetchone()[0]
    assert "group meeting" in json.loads(stored)


# --- onboarding -------------------------------------------------------------


def test_onboarding_sets_persists_across_seasons(conn, tmp_path):
    created = onboarding.create(
        conn,
        onboarding.Draft(
            url="https://calendar.google.com/calendar/ical/troop7%40group.calendar."
                "google.com/public/basic.ics",
            child_id="luke", sport="scouts", team_name="Troop Seven",
            kind="calendar-feed", tz="America/New_York",
        ),
        secrets=SecretStore(path=tmp_path / "secrets.json", environ={}),
    )
    config = repo.get_source(conn, created.source_id).config

    assert config["persists_across_seasons"] is True
    assert config["default_calendar"] == "practices", \
        "where this team's non-game events already go"
    assert created.source_id.startswith("cal-")


# --- the console ------------------------------------------------------------


@pytest.fixture
def console(tmp_path):
    from test_web import Client, Feed

    from calsync.web import app as web_app

    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text("{}")
    secrets_path.chmod(0o600)
    connection = db.open_db(tmp_path / "calsync.db")
    connection.execute("INSERT INTO children (id, name, initial, birth_order) "
                       "VALUES ('luke', 'Luke', 'Lu', 1)")
    connection.commit()
    connection.close()
    class HookedFeed(Feed):
        during = None

        def __call__(self, assembled, **kw):
            if self.during:
                self.during()
            return super().__call__(assembled, **kw)

    feed = HookedFeed(FIXTURE.read_bytes())
    app = web_app.create_app(tmp_path / "calsync.db",
                             secrets=SecretStore(path=secrets_path, environ={}),
                             fetcher=feed, clock=lambda: NOW)
    client = Client(app)
    client.feed = feed
    url = "https://calendar.google.com/calendar/ical/troop7%40group.calendar.google.com/public/basic.ics"
    inspected = client.post("/onboard", {"url": url})
    assert '<option value="calendar-feed" selected>' in inspected["body"], \
        "a Google export preselects this kind"
    made = client.post("/onboard/create", {
        "url": url, "team_name": "Explorers", "child": "luke", "sport": "scouts",
        "kind": "calendar-feed", "tz": "America/New_York", "poll_interval_s": "1200",
    })
    assert made["status"] == 303, made["body"][:1500]
    source_id = made["headers"]["Location"].split("/sources/")[1].split("?")[0]
    return client, tmp_path / "calsync.db", source_id


def test_source_page_lists_categories_with_a_destination_each(console):
    client, _, source_id = console
    page = client.get(f"/sources/{source_id}")["body"]

    assert 'id="categories"' in page
    assert "Padawan Meeting (new)" in page
    assert "Elders&#039; Roundtable (new)" in page or "Elders' Roundtable (new)" in page
    assert '<option value="skip"' in page and "New calendar…" in page


def test_choosing_a_category_stores_it_on_the_source(console):
    client, path, source_id = console
    reply = client.post(f"/sources/{source_id}/category",
                        {"label": "Council  Meeting", "destination": "skip"})
    assert reply["status"] == 303

    client.post(f"/sources/{source_id}/category",
                {"label": "Padawan Meeting", "destination": "__new__",
                 "new_calendar": "Padawans"})
    connection = db.connect(path)
    config = repo.get_source(connection, source_id).config
    assert config["categories"] == {"Council Meeting": "skip", "Padawan Meeting": "padawans"}
    page = client.get(f"/sources/{source_id}")["body"]
    assert "Council Meeting (new)" not in page


def test_review_lists_new_categories_apart_from_held_events(console):
    client, path, source_id = console
    connection = db.connect(path)
    repo.set_staging(connection, source_id, None)
    connection.close()

    page = client.get("/review")["body"]
    assert "New categories" in page
    assert "already on practices, confirm or change" in page
    assert "nothing has gone to the family" not in page


def test_changing_the_default_calendar_moves_unset_categories(console):
    client, path, source_id = console
    client.post(f"/sources/{source_id}/category",
                {"label": "Padawan Meeting", "destination": "practices"})
    refused = client.post(f"/sources/{source_id}/default-calendar", {"destination": "skip"})
    assert refused["status"] != 303

    reply = client.post(f"/sources/{source_id}/default-calendar",
                        {"destination": "__new__", "new_calendar": "Explorers"})
    assert reply["status"] == 303
    connection = db.connect(path)
    config = repo.get_source(connection, source_id).config
    assert config["default_calendar"] == "explorers"
    assert config["categories"] == {"Padawan Meeting": "practices"}, "set ones stay put"


def test_confirming_a_time_from_the_console(console):
    client, path, source_id = console
    question = "Padawan Meeting, Tue 24 Nov 2026 at 07:00 (usually 19:00)"
    reply = client.post(f"/sources/{source_id}/confirm-time", {"question": question})
    assert reply["status"] == 303
    connection = db.connect(path)
    assert repo.get_source(connection, source_id).config["times_confirmed"] == [question]


def _config(path, source_id):
    connection = db.connect(path)
    try:
        return repo.get_source(connection, source_id).config
    finally:
        connection.close()


def _hidden(page, name):
    import html
    return html.unescape(re.search(rf'name="{name}" value="([^"]*)"', page).group(1))


def test_category_row_names_what_placed_it(console):
    client, path, source_id = console
    client.post(f"/sources/{source_id}/category",
                {"label": "Mos Espa Camporee", "destination": "skip"})
    preview = client.post(f"/sources/{source_id}/rules/preview",
                          {"op": "add", "match": "contains", "text": "meeting",
                           "destination": "practices"})["body"]
    client.post(f"/sources/{source_id}/rules", {"rules": _hidden(preview, "rules")})
    preview = client.post(f"/sources/{source_id}/rules/preview",
                          {"op": "add", "match": "starts_with", "text": "council",
                           "destination": "practices"})["body"]
    client.post(f"/sources/{source_id}/rules", {"rules": _hidden(preview, "rules")})
    page = client.get(f"/sources/{source_id}")["body"]

    assert "set by you" in page
    assert "rule: contains &quot;meeting&quot;" in page
    assert "also starts with &quot;council&quot;" in page, \
        "a category two rules match names the first and the other"
    assert "Elders&#039; Roundtable (new)" in page


def test_rule_preview_lists_matches_without_saving(console):
    client, path, source_id = console
    reply = client.post(f"/sources/{source_id}/rules/preview",
                        {"op": "add", "match": "contains", "text": "Meeting",
                         "destination": "__new__", "new_calendar": "Meetings"})
    page = reply["body"]

    assert reply["status"] == 200, page[:800]
    assert "Council Meeting (11)" in page and "Group Meeting (1)" in page
    assert "events move on the next sync" in page
    assert "rules" not in _config(path, source_id), "a preview changes nothing"

    client.post(f"/sources/{source_id}/rules", {"rules": _hidden(page, "rules")})
    assert _config(path, source_id)["rules"] == [
        {"match": "contains", "text": "Meeting", "destination": "meetings"}]
    nothing = client.post(f"/sources/{source_id}/rules/preview",
                          {"op": "add", "match": "starts_with", "text": "Banquet",
                           "destination": "practices"})["body"]
    assert "Matches nothing in the window" in nothing


def test_reordering_or_deleting_a_rule_is_previewed(console):
    client, path, source_id = console
    two = [{"match": "contains", "text": "meeting", "destination": "meetings"},
           {"match": "starts_with", "text": "council", "destination": "council"}]
    client.post(f"/sources/{source_id}/rules", {"rules": json.dumps(two)})

    moved = client.post(f"/sources/{source_id}/rules/preview", {"op": "up", "index": "1"})["body"]
    assert re.search(r"Council Meeting</td>\s*<td>11</td>\s*<td>meetings</td>\s*<td>council</td>",
                     moved), "moving a rule up shows what it takes"
    assert _config(path, source_id)["rules"] == two

    deleted = client.post(f"/sources/{source_id}/rules/preview",
                          {"op": "delete", "index": "0"})["body"]
    assert "Padawan Meeting (new)" in deleted
    assert _config(path, source_id)["rules"] == two


def test_rules_are_not_previewed_or_saved_against_an_unreadable_feed(console):
    client, path, source_id = console
    client.feed.error = OSError("feed down")
    change = {"op": "add", "match": "contains", "text": "meeting", "destination": "skip"}

    assert client.post(f"/sources/{source_id}/rules/preview", change)["status"] != 200
    saved = client.post(f"/sources/{source_id}/rules", {"rules": json.dumps([
        {"match": "contains", "text": "meeting", "destination": "skip"}])})
    assert saved["status"] != 303
    assert "rules" not in _config(path, source_id)


def test_saving_rules_keeps_a_category_set_during_the_fetch(console):
    client, path, source_id = console

    def someone_else_sets_a_category():
        connection = db.connect(path)
        config = repo.get_source(connection, source_id).config
        config["categories"] = {"Council Meeting": "council"}
        connection.execute("UPDATE sources SET config = ? WHERE id = ?",
                           (json.dumps(config), source_id))
        connection.commit()
        connection.close()
        client.feed.during = None

    client.feed.during = someone_else_sets_a_category
    client.post(f"/sources/{source_id}/rules", {"rules": json.dumps([MEETINGS])})

    config = _config(path, source_id)
    assert config["rules"] == [MEETINGS]
    assert config["categories"] == {"Council Meeting": "council"}


def test_keep_all_refuses_labels_that_are_not_a_list(console):
    client, _, source_id = console
    assert client.post(f"/sources/{source_id}/keep-all", {"labels": "7"})["status"] != 303


def test_category_a_rule_stops_matching_is_new_without_a_push(console):
    client, path, source_id = console
    connection = db.connect(path)
    repo.set_staging(connection, source_id, None)
    connection.close()
    client.post(f"/sources/{source_id}/rules", {"rules": json.dumps([
        {"match": "contains", "text": "meeting", "destination": "practices"}])})
    connection = db.connect(path)
    connection.execute("UPDATE sources SET category_notified = NULL")
    connection.commit()
    connection.close()

    client.post(f"/sources/{source_id}/rules", {"rules": "[]"})
    connection = db.connect(path)
    stored = json.loads(connection.execute(
        "SELECT category_notified FROM sources WHERE id = ?", (source_id,)).fetchone()[0])
    assert {"padawan meeting", "council meeting", "group meeting"} <= set(stored), \
        "recorded as announced when saved, so no push follows"
    assert "Padawan Meeting" in client.get("/review")["body"], "but still listed as new"


def test_keep_all_leaves_categories_unset(console):
    client, path, source_id = console
    page = client.get(f"/sources/{source_id}")["body"]
    reply = client.post(f"/sources/{source_id}/keep-all",
                        {"labels": _hidden(page, "labels")})

    assert reply["status"] == 303
    config = _config(path, source_id)
    assert config["categories"] == {}, "no destination is set"
    assert "Padawan Meeting" in config["categories_seen"]
    page = client.get(f"/sources/{source_id}")["body"]
    assert "(new)" not in page
    assert "Keep all" not in page


def test_rule_added_after_keep_all_routes_those_categories(console):
    client, path, source_id = console
    page = client.get(f"/sources/{source_id}")["body"]
    client.post(f"/sources/{source_id}/keep-all", {"labels": _hidden(page, "labels")})
    client.post(f"/sources/{source_id}/rules", {"rules": json.dumps([
        {"match": "contains", "text": "meeting", "destination": "meetings"}])})

    config = _config(path, source_id)
    assert routing.placement("Council Meeting", config) == ("meetings", "rule", 0)
    assert routing.placement("Mos Espa Camporee", config)[0] == "practices"


def test_keep_all_answers_only_the_categories_shown(console):
    client, path, source_id = console
    connection = db.connect(path)
    repo.set_staging(connection, source_id, None)
    connection.close()
    review = client.get("/review")["body"]
    assert "Keep all of" in review
    client.post(f"/sources/{source_id}/keep-all",
                {"labels": json.dumps(["Padawan Meeting", "Council Meeting"]),
                 "back": "review"})

    page = client.get(f"/sources/{source_id}")["body"]
    assert "Padawan Meeting (new)" not in page
    assert "Group Meeting (new)" in page, "one the page did not show stays new"


def test_default_places_are_set_from_the_source_page(console):
    client, path, source_id = console
    connection = db.connect(path)
    hall = _hall(connection)
    repo.set_staging(connection, source_id, None)
    connection.close()

    client.post(f"/sources/{source_id}/category",
                {"label": "Group Meeting", "destination": "practices", "place": str(hall)})
    assert _config(path, source_id)["places"] == {"Group Meeting": hall}
    client.post(f"/sources/{source_id}/category",
                {"label": "Group Meeting", "destination": "practices"})
    assert _config(path, source_id)["places"] == {"Group Meeting": hall}, \
        "a form without a place leaves it alone"
    refused = client.post(f"/sources/{source_id}/category",
                          {"label": "Group Meeting", "destination": "practices",
                           "place": "999"})
    assert refused["status"] != 303

    preview = client.post(f"/sources/{source_id}/rules/preview",
                          {"op": "add", "match": "contains", "text": "meeting",
                           "destination": "practices", "place": str(hall)})["body"]
    assert "at Naboo Scout Hall when the feed gives no place" in preview
    client.post(f"/sources/{source_id}/rules", {"rules": _hidden(preview, "rules")})
    assert _config(path, source_id)["rules"][0]["place"] == hall
    unknown = client.post(f"/sources/{source_id}/rules", {"rules": json.dumps([
        {"match": "contains", "text": "meeting", "destination": "practices", "place": 999}])})
    assert unknown["status"] != 303
    assert _config(path, source_id)["rules"][0]["place"] == hall


def test_route_category_task_dispatched_per_new_category(conn, target):
    from calsync import enrichment

    _configure(conn, categories={"Padawan Meeting": "padawans"},
               rules=[{"match": "starts_with", "text": "council", "destination": "council"}])
    report = _sync(conn, target)
    config = repo.get_source(conn, "cal-explorers").config
    routed = [t for t in enrichment.tasks("cal-explorers", report, config=config)
              if t.type == "route_category"]

    assert sorted(t.context for t in routed) == [
        ("Elders' Roundtable",), ("Group Meeting",), ("Mos Espa Camporee",)]
    assert {"explorers", "padawans", "council", "skip"} <= set(routed[0].candidates)
    assert not any(t.type == "route_category" for t in enrichment.tasks(
        "cal-explorers", report, config=config, staged=True)), "staged: the person's to answer"
    assert "new_categories" not in report.diagnostics, "news, not a diagnostic"


def _route_task(conn, target, label, answer):
    from calsync import enrichment

    report = _sync(conn, target)
    config = repo.get_source(conn, "cal-explorers").config
    (task,) = [t for t in enrichment.tasks("cal-explorers", report, config=config)
               if t.type == "route_category" and t.context == (label,)]
    return repo.Task(id=task.id, source_id="cal-explorers", kind=task.kind, type=task.type,
                     context=task.context, candidates=task.candidates, state="approved",
                     answer=answer)


def test_approved_route_category_writes_the_category(conn, target):
    from calsync import enrichment

    hall = _hall(conn)
    task = _route_task(conn, target, "Group Meeting",
                       {"destination": "Gatherings", "place": hall})
    enrichment.apply(conn, task)

    config = repo.get_source(conn, "cal-explorers").config
    assert config["categories"] == {"Group Meeting": "gatherings"}
    assert config["places"] == {"Group Meeting": hall}
    _sync(conn, target)
    assert _live(conn)[GROUP].collection == "gatherings"
    assert _venue_of(conn, GROUP) == "Naboo Scout Hall"

    late = _route_task(conn, target, "Mos Espa Camporee", {"destination": "skip"})
    _configure(conn, categories={**repo.get_source(conn, "cal-explorers").config["categories"],
                                 "Mos Espa Camporee": "explorers"})
    with pytest.raises(enrichment.AnswerError, match="placed by hand"):
        enrichment.apply(conn, late)
    assert repo.get_source(conn, "cal-explorers").config["categories"][
        "Mos Espa Camporee"] == "explorers", "a person's choice stands"

    with pytest.raises(enrichment.AnswerError, match="no venue"):
        enrichment.apply(conn, _route_task(conn, target, "Elders' Roundtable",
                                           {"destination": "skip", "place": 999}))


def test_route_category_answer_shape_is_in_the_contract():
    from calsync import enrichment
    from calsync.api import contract

    enrichment.validate("route_category", contract.ANSWER_SHAPES["route_category"]["answer"])
    enrichment.validate("route_category", {"destination": "skip"})
    for bad in ({}, {"destination": " "}, {"destination": "x", "place": "7"},
                {"destination": "x", "place": True}):
        with pytest.raises(enrichment.AnswerError):
            enrichment.validate("route_category", bad)


def test_approve_all_applies_a_sources_answers(console):
    from calsync import enrichment

    client, path, source_id = console
    connection = db.connect(path)
    hall = _hall(connection)
    answers = {"Group Meeting": {"destination": "practices", "place": hall},
               "Council Meeting": {"destination": "skip"},
               "Mos Espa Camporee": {"destination": "camp", "place": 999}}
    for label, answer in answers.items():
        task_id = f"task_{label[:4].lower()}"
        repo.record_task(connection, task_id=task_id, source_id=source_id,
                         kind="new_categories", type="route_category", context=(label,),
                         candidates=(), dispatched_at="2026-09-01T12:00:00+00:00")
        enrichment.validate("route_category", answer)
        repo.record_answer(connection, task_id=task_id, answer=answer, rationale=None,
                           answered_by="agent", answered_at="2026-09-01T12:05:00+00:00")
    connection.commit()
    connection.close()

    page = client.get("/review")["body"]
    assert "Approve all 3 for Explorers" in page
    reply = client.post(f"/review/sources/{source_id}/approve-all",
                        {"tasks": _hidden(page, "tasks")})
    assert reply["status"] == 303
    assert "Approved%202" in reply["headers"]["Location"]

    config = _config(path, source_id)
    assert config["categories"] == {"Group Meeting": "practices", "Council Meeting": "skip"}
    assert config["places"] == {"Group Meeting": hall}
    connection = db.connect(path)
    states = {t.context[0]: t.state for t in repo.list_tasks(connection)}
    assert states == {"Group Meeting": "approved", "Council Meeting": "approved",
                      "Mos Espa Camporee": "answered"}, "one that cannot apply still waits"


def test_an_agent_can_propose_a_time_is_right(conn, target):
    from calsync import enrichment

    report = _sync(conn, target, raw=_with_slip())
    (task,) = [t for t in enrichment.tasks("cal-explorers", report) if t.kind == "suspect_times"]
    assert task.type == "confirm_time"

    with pytest.raises(enrichment.AnswerError):
        enrichment.validate("confirm_time", {"time_is_right": False})

    approved = repo.Task(id=task.id, source_id="cal-explorers", kind=task.kind,
                         type=task.type, context=task.context, candidates=(),
                         state="approved", answer={"time_is_right": True})
    enrichment.apply(conn, approved)
    assert repo.get_source(conn, "cal-explorers").config["times_confirmed"] == list(task.context)
    assert "suspect_times" not in _sync(conn, target, raw=_with_slip()).diagnostics
