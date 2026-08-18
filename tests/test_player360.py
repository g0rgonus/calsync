"""Golden tests against the real Player360 payload.

Every assertion here encodes a finding from docs/sources/player360.md. If one
of these fails after a feed-format change, the doc is the place to look first.
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

VANGUARD = Activity(
    id="jesse-soccer-vanguard",
    child_id="jesse",
    name="Vanguard",
    official_name="U10PL",
    short_name="Vanguard",
    league="PSL",
    age_group="U10",
    home_venue="Thistledown Park",
    aliases=("U10PL", "U10 DA", "Vanguard", "Vanguard Academy"),
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

JESSE = Child(id="jesse", name="Jesse", initial="J", birth_order=2)
PARKER = Child(id="parker", name="Parker", initial="P", birth_order=1)
MIRA = Child(id="mira", name="Mira", initial="M", birth_order=3)


@pytest.fixture(scope="module")
def result():
    return player360.parse_feed(
        FIXTURE.read_bytes(), VANGUARD, source_id="p360-jesse-vanguard"
    )


@pytest.fixture(scope="module")
def by_uid(result):
    return {e.uid: e for e in result.events}


FESTIVAL = "360Player-event-4716716"
MINICAMP = "360Player-event-4801206"
PRACTICE = "360Player-event-4801208"
MATCH = "360Player-event-4823901"


def test_all_events_parsed(result):
    assert len(result.events) == 4


def test_is_game_comes_from_categories(by_uid):
    assert by_uid[FESTIVAL].is_game is True
    assert by_uid[MATCH].is_game is True
    assert by_uid[MINICAMP].is_game is False
    assert by_uid[PRACTICE].is_game is False


def test_routing_follows_is_game(by_uid):
    assert by_uid[MATCH].collection == "games"
    assert by_uid[PRACTICE].collection == "practices"


def test_league_match_yields_opponent_with_age_suffix_stripped(by_uid):
    # "U10PL PSL Match vs Harbour FC U10" -> opponent "Harbour FC"
    assert by_uid[MATCH].opponent == "Harbour FC"
    assert by_uid[MATCH].detail is None


def test_practice_summary_strips_our_own_team_token(by_uid):
    # "U10PL Practice" -> "Practice". This is what makes the user-requested
    # "Jesse ⚽️ Practice" fall out of the upstream-wins rule.
    assert by_uid[PRACTICE].detail == "Practice"
    assert by_uid[PRACTICE].opponent is None


def test_club_event_keeps_its_own_label(by_uid):
    assert by_uid[FESTIVAL].detail == "Super 8v8 Festival Kickoff"
    assert by_uid[MINICAMP].detail == "Club Minicamp Kickoff"


def test_venue_split_on_street_number(by_uid):
    venue = by_uid[MATCH].venue
    assert venue.name == "Thistledown Park"
    assert venue.address == "1009 Thistledown Rd, Marbury NX 40114"

    # The other venue uses a comma before the state; the split must not care.
    festival = by_uid[FESTIVAL].venue
    assert festival.name == "Alder Reach Memorial Park"
    assert festival.address == "7160 Kestrel Ln, Fenwick, NX 40219"


def test_no_coordinates_are_invented(by_uid):
    assert by_uid[MATCH].venue.resolved is False


def test_home_away_derived_from_venue_not_the_vs_string(by_uid):
    # SUMMARY always reads "vs"; Thistledown Park is the home ground.
    assert by_uid[MATCH].home is True


def test_description_dropped_when_it_duplicates_summary(by_uid):
    assert by_uid[MATCH].body is None            # identical to SUMMARY
    assert by_uid[MINICAMP].body == "Vanguard Academy Minicamp"  # differs, kept


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

    a = player360.parse_feed(original, VANGUARD, source_id="s")
    b = player360.parse_feed(churned, VANGUARD, source_id="s")
    hashes_a = {e.uid: e.content_hash for e in a.events}
    hashes_b = {e.uid: e.content_hash for e in b.events}
    assert hashes_a == hashes_b


def test_content_hash_reacts_to_a_real_change():
    original = FIXTURE.read_text()
    moved = original.replace("DTSTART:20260829T140000Z", "DTSTART:20260829T150000Z")
    a = {e.uid: e.content_hash for e in player360.parse_feed(original, VANGUARD, source_id="s").events}
    b = {e.uid: e.content_hash for e in player360.parse_feed(moved, VANGUARD, source_id="s").events}
    assert a[MATCH] != b[MATCH]
    assert a[PRACTICE] == b[PRACTICE]  # untouched events must not churn


def test_empty_feed_is_not_evidence_of_cancellation():
    empty = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x\r\nEND:VCALENDAR\r\n"
    with pytest.raises(player360.FeedError, match="cancellation"):
        player360.parse_feed(empty, VANGUARD, source_id="s")


def test_garbage_feed_raises_rather_than_returning_nothing():
    with pytest.raises(player360.FeedError):
        player360.parse_feed("not a calendar at all", VANGUARD, source_id="s")


# --- titles -----------------------------------------------------------------


def _title(event, kids, settings=None):
    return title_norm.render(event, VANGUARD, kids, settings or SETTINGS)


def test_titles(by_uid):
    assert _title(by_uid[MATCH], [JESSE]) == "Jesse ⚽️ vs Harbour FC"
    assert _title(by_uid[PRACTICE], [JESSE]) == "Jesse ⚽️ Practice"
    assert _title(by_uid[FESTIVAL], [JESSE]) == "Jesse ⚽️ Super 8v8 Festival Kickoff"


def test_multi_kid_titles_use_initials_in_birth_order(by_uid):
    event = by_uid[PRACTICE]
    assert _title(event, [JESSE, PARKER]) == "P+J ⚽️ Practice"
    # Order of the input list must not matter; birth order decides.
    assert _title(event, [PARKER, JESSE]) == "P+J ⚽️ Practice"
    assert _title(event, [JESSE, PARKER, MIRA]) == "Kids ⚽️ Practice"


def test_title_omits_venue(by_uid):
    assert "Thistledown" not in _title(by_uid[MATCH], [JESSE])


def test_single_kid_titles_fit_week_view(by_uid):
    """First 12 chars must answer 'which kid, which sport'."""
    for event in by_uid.values():
        head = _title(event, [JESSE])[:12]
        assert head.startswith("Jesse ⚽️")


def test_away_only_marked_when_positively_known(by_uid):
    event = by_uid[MATCH]
    event_unknown = type(event)(**{**event.__dict__, "home": None})
    # Unknown must not silently render as "@" — the feed always says "vs".
    assert _title(event_unknown, [JESSE]) == "Jesse ⚽️ vs Harbour FC"

    event_away = type(event)(**{**event.__dict__, "home": False})
    assert _title(event_away, [JESSE]) == "Jesse ⚽️ @ Harbour FC"


# --- diff / disappearance guard ---------------------------------------------

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def test_diff_classifies_new_changed_and_unchanged(result):
    known = {e.uid: e.content_hash for e in result.events}
    known[MATCH] = "stale-hash"
    del known[FESTIVAL]

    d = diff_poll(result.events, known, now=NOW)
    assert [e.uid for e in d.created] == [FESTIVAL]
    assert [e.uid for e in d.updated] == [MATCH]
    assert len(d.unchanged) == 2
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
    assert re.search(r"4 of 4", d.anomaly)


def test_guard_trips_on_count_even_when_percentage_is_low(result):
    known = {e.uid: e.content_hash for e in result.events}
    for i in range(100):
        known[f"ghost-{i}"] = "x"
    d = diff_poll(result.events, known, now=NOW)
    assert d.is_anomalous
    assert d.cancelled == []
