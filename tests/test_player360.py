"""Golden tests against the real Player360 payload.

Every assertion here encodes something the feed was found to do
(docs/connectors/360player.md). If one fails after a feed-format change, start
there.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from calsync.db import DEFAULT_SETTINGS
from calsync.diff import diff_poll
from calsync.models import Activity, Child
from calsync.normalize import title as title_norm
from calsync.settings import Settings
from calsync.sources import player360

FIXTURE = Path(__file__).parent / "fixtures" / "player360_sample.ics"

JEDI = Activity(
    id="luke-soccer-jedi",
    child_id="luke",
    name="Jedi",
    official_name="U10RS",
    short_name="Jedi",
    league="RSL",
    age_group="U10",
    home_venue="Theed Park",
    aliases=("U10RS", "U10 RS", "Jedi", "Jedi Academy"),
    sport="soccer",
    emoji="⚽️",
    tz="America/New_York",
)

def make_settings(**overrides):
    raw = {**DEFAULT_SETTINGS, **{k: str(v) for k, v in overrides.items()}}
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany("INSERT INTO settings VALUES (?, ?)", list(raw.items()))
    return Settings.load(conn)


SETTINGS = make_settings()

LUKE = Child(id="luke", name="Luke", initial="Lu", birth_order=2)
LEIA = Child(id="leia", name="Leia", initial="Le", birth_order=1)
REY = Child(id="rey", name="Rey", initial="R", birth_order=3)


@pytest.fixture(scope="module")
def result():
    return player360.parse_feed(
        FIXTURE.read_bytes(), JEDI, source_id="p360-luke-jedi"
    )


@pytest.fixture(scope="module")
def by_uid(result):
    return {e.uid: e for e in result.events}


FESTIVAL = "360Player-event-7716716"
MINICAMP = "360Player-event-7801206"
PRACTICE = "360Player-event-7801208"
MATCH = "360Player-event-7823901"
AWAY_MATCH = "360Player-event-7823977"


def test_all_events_parsed(result):
    assert len(result.events) == 5


def test_is_game_comes_from_categories(by_uid):
    assert by_uid[FESTIVAL].is_game is True
    assert by_uid[MATCH].is_game is True
    assert by_uid[MINICAMP].is_game is False
    assert by_uid[PRACTICE].is_game is False


def test_routing_follows_is_game(by_uid):
    assert by_uid[MATCH].collection == "games"
    assert by_uid[PRACTICE].collection == "practices"


def test_league_match_yields_opponent_with_age_suffix_stripped(by_uid):
    # "U10RS RSL Match vs Mos Eisley FC U10" -> opponent "Mos Eisley FC"
    assert by_uid[MATCH].opponent == "Mos Eisley FC"
    assert by_uid[MATCH].detail is None


def test_practice_summary_strips_our_own_team_token(by_uid):
    # "U10RS Practice" -> "Practice". This is what makes the user-requested
    # "Luke ⚽️ Practice" fall out of the upstream-wins rule.
    assert by_uid[PRACTICE].detail == "Practice"
    assert by_uid[PRACTICE].opponent is None


def test_club_event_keeps_its_own_label(by_uid):
    assert by_uid[FESTIVAL].detail == "Super 8v8 Festival Kickoff"
    assert by_uid[MINICAMP].detail == "Club Minicamp Kickoff"


def test_venue_split_on_street_number(by_uid):
    venue = by_uid[MATCH].venue
    assert venue.name == "Theed Park"
    assert venue.address == "1009 Theed Rd, Naboo NX 40114"

    # The other venue uses a comma before the state; the split must not care.
    festival = by_uid[FESTIVAL].venue
    assert festival.name == "Jedi Temple Memorial Park"
    assert festival.address == "7160 Senate Ln, Coruscant, NX 40219"


def test_no_coordinates_are_invented(by_uid):
    assert by_uid[MATCH].venue.resolved is False


def test_home_away_derived_from_venue_not_the_vs_string(by_uid):
    # SUMMARY always reads "vs"; Theed Park is the home ground.
    assert by_uid[MATCH].home is True


def test_an_away_match_is_read_from_the_at_marker(by_uid):
    """A third SUMMARY shape: "U10RS RSL Match @ Felucia Town SC".

    Observed live on a U10 club feed, where every away fixture reads this way.
    The pattern required `vs`, so the whole string fell through to the token
    stripper: no opponent was recovered, home/away was never set, and the title
    rendered the coach's raw text — "Luke ⚽️ U10RS RSL Match @ Felucia
    Town SC" instead of "Luke ⚽️ @ Felucia Town SC".
    """
    event = by_uid[AWAY_MATCH]
    assert event.opponent == "Felucia Town SC"
    assert event.detail is None
    assert event.is_game is True


def test_the_at_marker_is_what_makes_it_away(by_uid):
    """`@` outranks the venue check, because only `@` states the fixture.

    The venue here is not the home ground, so both agree — but the marker is
    what carries it, and many activities have no `home_venue` configured at all,
    which leaves the venue check answering None forever.
    """
    assert by_uid[AWAY_MATCH].home is False


def test_a_vs_match_still_defers_to_the_venue(by_uid):
    """`vs` says nothing: this feed writes it regardless of where it is played.

    Reading it as "home" would mark every away fixture on a feed that only ever
    writes `vs` — which is the trap `venue.matches_home` was written around.
    """
    assert by_uid[MATCH].home is True, "Theed Park is the home ground"


def test_description_dropped_when_it_duplicates_summary(by_uid):
    assert by_uid[MATCH].body is None            # identical to SUMMARY
    assert by_uid[MINICAMP].body == "Jedi Academy Minicamp"  # differs, kept


def test_times_are_utc_and_local_start_uses_venue_zone(by_uid):
    match = by_uid[MATCH]
    assert match.starts_at == datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc)
    # 14:00Z is a 10:00 EDT kickoff — the number a parent plans around.
    assert match.local_start.hour == 10
    assert match.local_start.tzinfo.key == "America/New_York"


def test_content_hash_ignores_sequence_and_last_modified():
    """Player360 touches every event 2-3s after DTEND, bumping SEQUENCE and
    LAST-MODIFIED. Hashing those would re-push every event the night it ran."""
    original = FIXTURE.read_text()
    churned = original.replace("SEQUENCE:1786731300", "SEQUENCE:1786999999").replace(
        "LAST-MODIFIED:20260810T101500Z", "LAST-MODIFIED:20260829T150003Z"
    )
    assert churned != original

    a = player360.parse_feed(original, JEDI, source_id="s")
    b = player360.parse_feed(churned, JEDI, source_id="s")
    hashes_a = {e.uid: e.content_hash for e in a.events}
    hashes_b = {e.uid: e.content_hash for e in b.events}
    assert hashes_a == hashes_b


def test_content_hash_reacts_to_a_real_change():
    original = FIXTURE.read_text()
    moved = original.replace("DTSTART:20260829T140000Z", "DTSTART:20260829T150000Z")
    a = {e.uid: e.content_hash for e in player360.parse_feed(original, JEDI, source_id="s").events}
    b = {e.uid: e.content_hash for e in player360.parse_feed(moved, JEDI, source_id="s").events}
    assert a[MATCH] != b[MATCH]
    assert a[PRACTICE] == b[PRACTICE]  # untouched events must not churn


def test_empty_feed_is_not_evidence_of_cancellation():
    empty = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x\r\nEND:VCALENDAR\r\n"
    with pytest.raises(player360.FeedError, match="cancellation"):
        player360.parse_feed(empty, JEDI, source_id="s")


def test_garbage_feed_raises_rather_than_returning_nothing():
    with pytest.raises(player360.FeedError):
        player360.parse_feed("not a calendar at all", JEDI, source_id="s")


# --- titles -----------------------------------------------------------------


def _title(event, kids, settings=None):
    return title_norm.render(event, JEDI, kids, settings or SETTINGS)


def test_titles(by_uid):
    assert _title(by_uid[MATCH], [LUKE]) == "Luke ⚽️ vs Mos Eisley FC"
    assert _title(by_uid[PRACTICE], [LUKE]) == "Luke ⚽️ Practice"
    assert _title(by_uid[FESTIVAL], [LUKE]) == "Luke ⚽️ Super 8v8 Festival Kickoff"


GUEST_FIXTURE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//360 Player/Event calendar
BEGIN:VEVENT
UID:360Player-event-7830512
SUMMARY:ORSL Match vs Cloud City FC 2015/2016
DTSTAMP:20260814T101500Z
DTSTART:20260913T170000Z
DTEND:20260913T180000Z
LOCATION:Tosche Elementary School\\, 4410 Dune Road Bestine\\, NX
DESCRIPTION:Bring both kits as back up\\, but plan to wear the blue kit.
URL:https://app.360player.com/organization/100200/events/7830512
CATEGORIES:match
LAST-MODIFIED:20260814T101500Z
CREATED:20260814T101500Z
SEQUENCE:1787076900
END:VEVENT
END:VCALENDAR
"""


def test_a_guest_fixture_with_no_team_token_still_parses():
    """A player's own feed carries games they were invited to with another squad.

    Observed live: "ORSL Match vs FC Kashyyyk 2015/2016" on a U10 player's feed
    for a U11 game — league and type with no team token in front, and the
    opponent's birth-year band typed in by the manager. The pattern required a
    team token, so the title rendered the coach's raw text.
    """
    [event] = player360.parse_feed(
        GUEST_FIXTURE, JEDI, source_id="p360-luke-jedi"
    ).events
    assert event.opponent == "Cloud City FC"
    assert event.detail is None
    assert event.home is False, "not the home ground, so `vs` defers to the venue"
    assert _title(event, [LUKE]) == "Luke ⚽️ @ Cloud City FC"


@pytest.mark.parametrize("summary, opponent", [
    ("ORSL Match vs Cloud City FC 2015/2016", "Cloud City FC"),
    ("ORSL Match vs Cloud City FC 2015/16", "Cloud City FC"),
    ("ORSL Match vs Cloud City FC 2016", "Cloud City FC"),
    ("U10RS RSL Match @ Cloud City FC 2016", "Cloud City FC"),
    # Only a trailing 20xx band is a birth year; anything else is the name.
    ("ORSL Match vs Cloud 2016 City FC", "Cloud 2016 City FC"),
    ("ORSL Match vs Cloud City 1904", "Cloud City 1904"),
    # A different age band is still kept, as before.
    ("U10RS RSL Match vs Cloud City FC U11", "Cloud City FC U11"),
])
def test_opponent_birth_year_band_is_dropped(summary, opponent):
    from calsync.normalize import summary as summary_norm
    got, _, _ = summary_norm.parse(summary, age_group="U10")
    assert got == opponent


def test_a_guest_fixture_is_not_evidence_of_our_team_name():
    """Onboarding reads the left of a league match as our team. A guest fixture
    has nothing there, so it must say nothing rather than crash or guess."""
    from calsync import inspection
    assert inspection.fixture_sides("ORSL Match vs Cloud City FC 2015/2016") is None
    assert inspection.fixture_sides("U10RS RSL Match vs Mos Eisley FC U10") == (
        "U10RS", "Mos Eisley FC U10"
    )


def test_multi_kid_titles_use_initials_in_birth_order(by_uid):
    event = by_uid[PRACTICE]
    assert _title(event, [LUKE, LEIA]) == "Le+Lu ⚽️ Practice"
    # Order of the input list must not matter; birth order decides.
    assert _title(event, [LEIA, LUKE]) == "Le+Lu ⚽️ Practice"
    assert _title(event, [LUKE, LEIA, REY]) == "Kids ⚽️ Practice"


def test_title_omits_venue(by_uid):
    assert "Theed" not in _title(by_uid[MATCH], [LUKE])


def test_single_kid_titles_fit_week_view(by_uid):
    """First 12 chars must answer 'which kid, which sport'."""
    for event in by_uid.values():
        head = _title(event, [LUKE])[:12]
        assert head.startswith("Luke ⚽️")


def test_away_only_marked_when_positively_known(by_uid):
    event = by_uid[MATCH]
    event_unknown = type(event)(**{**event.__dict__, "home": None})
    # Unknown must not silently render as "@" — the feed always says "vs".
    assert _title(event_unknown, [LUKE]) == "Luke ⚽️ vs Mos Eisley FC"

    event_away = type(event)(**{**event.__dict__, "home": False})
    assert _title(event_away, [LUKE]) == "Luke ⚽️ @ Mos Eisley FC"


# --- diff / disappearance guard ---------------------------------------------

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def test_diff_classifies_new_changed_and_unchanged(result):
    known = {e.uid: e.content_hash for e in result.events}
    known[MATCH] = "stale-hash"
    del known[FESTIVAL]

    d = diff_poll(result.events, known, now=NOW)
    assert [e.uid for e in d.created] == [FESTIVAL]
    assert [e.uid for e in d.updated] == [MATCH]
    # Everything the fixture holds bar the one created and the one changed.
    # Derived rather than a literal, so growing the fixture does not fail a
    # test that is really asserting "the rest were left alone".
    assert len(d.unchanged) == len(result.events) - 2
    assert d.cancelled == []


def test_single_disappearance_is_a_cancellation(result):
    known = {e.uid: e.content_hash for e in result.events}
    known["360Player-event-ghost"] = "whatever"
    d = diff_poll(result.events, known, now=NOW)
    assert d.cancelled == ["360Player-event-ghost"]
    assert not d.is_anomalous


def test_mass_disappearance_is_held_not_processed(result):
    known = {e.uid: e.content_hash for e in result.events}
    d = diff_poll([], known, now=NOW)
    assert d.is_anomalous
    assert d.cancelled == []          # nothing is deleted
    assert re.search(rf"{len(known)} of {len(known)}", d.anomaly)


def test_guard_trips_on_count_even_when_percentage_is_low(result):
    known = {e.uid: e.content_hash for e in result.events}
    for i in range(100):
        known[f"ghost-{i}"] = "x"
    d = diff_poll(result.events, known, now=NOW)
    assert d.is_anomalous
    assert d.cancelled == []


# --- STATUS:CANCELLED ---------------------------------------------------------
# The export now says so.


def _with_status(uid: str, status: str) -> bytes:
    text = FIXTURE.read_text()
    marker = f"UID:{uid}\r\n" if f"UID:{uid}\r\n" in text else f"UID:{uid}\n"
    return text.replace(marker, f"{marker}STATUS:{status}\r\n").encode()


def test_a_cancelled_event_is_carried_as_cancelled():
    parsed = player360.parse_feed(
        _with_status(MATCH, "CANCELLED"), JEDI, source_id="p360-luke-jedi"
    )
    flags = {e.uid: e.cancelled for e in parsed.events}
    assert flags[MATCH] is True
    assert not any(v for uid, v in flags.items() if uid != MATCH)


def test_a_confirmed_event_is_not_cancelled():
    parsed = player360.parse_feed(
        _with_status(MATCH, "CONFIRMED"), JEDI, source_id="p360-luke-jedi"
    )
    assert not any(e.cancelled for e in parsed.events)


def test_the_status_is_not_part_of_the_content_hash(by_uid):
    """A cancelled event never reaches the content comparison, and one that
    comes back is new again — so the hash has no reason to move."""
    parsed = player360.parse_feed(
        _with_status(MATCH, "CANCELLED"), JEDI, source_id="p360-luke-jedi"
    )
    again = {e.uid: e for e in parsed.events}
    assert again[MATCH].content_hash == by_uid[MATCH].content_hash


def test_a_feed_cancellation_skips_the_guard_entirely(result):
    """Every tracked event called off at once is still not an anomaly: the feed
    parsed, and said so about each of them."""
    known = {e.uid: e.content_hash for e in result.events}
    d = diff_poll([], known, now=NOW, called_off=list(known))
    assert not d.is_anomalous
    assert sorted(d.called_off) == sorted(known)
    assert d.cancelled == []


def test_feed_cancellations_are_left_out_of_the_guards_arithmetic(result):
    """Two called off and one vanished, out of five: the guard measures one
    absence against three tracked events, not three against five."""
    events = result.events
    known = {e.uid: e.content_hash for e in events}
    called_off = [events[0].uid, events[1].uid]
    vanished = events[2].uid
    present = [e for e in events[3:]]

    d = diff_poll(present, known, now=NOW, called_off=called_off,
                  max_pct=0.5, max_count=3)
    assert not d.is_anomalous
    assert d.cancelled == [vanished]
    assert sorted(d.called_off) == sorted(called_off)


def test_a_held_disappearance_does_not_hold_feed_cancellations(result):
    events = result.events
    known = {e.uid: e.content_hash for e in events}
    d = diff_poll([], known, now=NOW, called_off=[events[0].uid])
    assert d.anomaly_kind == "disappearance"
    assert d.called_off == [events[0].uid]
    assert events[0].uid not in d.held_cancellations


def test_an_untracked_feed_cancellation_is_nothing_to_do(result):
    d = diff_poll(result.events, {e.uid: e.content_hash for e in result.events},
                  now=NOW, called_off=["never-written"])
    assert d.called_off == []
