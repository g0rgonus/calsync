"""Golden tests against the real TeamReach payload.

Every assertion encodes a finding from docs/sources/teamreach.md. The feed is
thin — no CATEGORIES, LOCATION, DESCRIPTION, URL or STATUS — so nearly all of
this is about recovering meaning from one coach-typed SUMMARY without inventing
anything that isn't there.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from calsync.models import Activity
from calsync.sources import parse
from calsync.sources.teamreach import (
    FeedError,
    classify,
    clean_venue,
    content_hash,
    parse_feed,
    parse_summary,
)

FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_sample.ics"

ACTIVITY = Activity(
    id="kid-teamreach",
    child_id="kid",
    name="Inter Tempest",
    sport="soccer",
    emoji="⚽️",
    tz="America/New_York",
)


@pytest.fixture(scope="module")
def result():
    return parse_feed(FIXTURE.read_bytes(), ACTIVITY, source_id="tr-tempest")


@pytest.fixture(scope="module")
def by_uid(result):
    return {e.uid: e for e in result.events}


# --- structure --------------------------------------------------------------


def test_all_events_parsed(result):
    assert len(result.events) == 23
    assert result.raw_sha256


def test_uid_is_stable_and_passed_through(by_uid):
    """<digits>@teamreach, unique per event — no generation timestamp.

    This is the good case; contrast docs/sources/flag-football.md.
    """
    assert len(by_uid) == 23
    assert all(uid.endswith("@teamreach") for uid in by_uid)
    assert "24253410@teamreach" in by_uid


def test_registry_dispatches_by_kind():
    result = parse(
        "teamreach", FIXTURE.read_bytes(), ACTIVITY, source_id="tr-tempest"
    )
    assert len(result.events) == 23


def test_times_are_utc(by_uid):
    event = by_uid["24253410@teamreach"]
    assert event.starts_at == datetime(2026, 3, 3, 23, 0, tzinfo=timezone.utc)
    assert all(e.starts_at.tzinfo is not None for e in by_uid.values())


# --- game vs practice, from the summary alone -------------------------------


def test_practice_and_game_are_distinguished_without_categories(by_uid):
    assert by_uid["24253410@teamreach"].is_game is False   # "Practice - Windmere "
    assert by_uid["24610930@teamreach"].is_game is True    # "Game - Kingsmere #2"


@pytest.mark.parametrize(
    "label, expected",
    [
        ("Game", True),
        ("Playoff Game", True),
        ("Make Up Game", True),
        ("Rescheduled Playoff Game", True),
        ("Playoff Game2", True),      # observed typo must not become a practice
        ("Practice", False),
        ("Training", False),
    ],
)
def test_event_type_vocabulary(label, expected):
    assert classify(label) is expected


def test_unrecognised_type_falls_to_practice_and_is_surfaced():
    """A mis-filed practice is a smaller error than a missed game."""
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//TeamReach//EN\r\n"
        "BEGIN:VEVENT\r\nUID:1@teamreach\r\nDTSTART:20260303T230000Z\r\n"
        "DTEND:20260304T000000Z\r\nSUMMARY:Team Photo - Windmere\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    result = parse_feed(ics, ACTIVITY, source_id="x")

    assert result.events[0].is_game is False
    assert result.unknown_types == ["Team Photo"]


# --- the coach-typed summary ------------------------------------------------


@pytest.mark.parametrize(
    "summary, event_type, venue",
    [
        ("Practice - Windmere ", "Practice", "Windmere"),
        ("Practice  - Windmere ", "Practice", "Windmere"),      # doubled space
        ("Practice- Windmere", "Practice", "Windmere"),          # no leading space
        ("Game - Kingsmere#2", "Game", "Kingsmere #2"),        # missing # space
        ("Playoff Game - Windmere.  6pm", "Playoff Game", "Windmere"),
        ("Make Up Game- Windmere ", "Make Up Game", "Windmere"),
    ],
)
def test_summary_variants_normalize_to_the_same_fields(summary, event_type, venue):
    parsed = parse_summary(summary)
    assert parsed.event_type == event_type
    assert clean_venue(parsed.venue_text) == venue


def test_venue_variants_collapse_to_one_name(by_uid):
    """"Kingsmere #2" and "Kingsmere#2" are the same place in the same feed."""
    venues = {e.venue.name for e in by_uid.values() if e.venue}
    assert venues == {"Windmere", "Ashgrove", "Kingsmere"}


def test_field_designator_is_split_off_the_venue(by_uid):
    """One park, one pin, many fields.

    Folding "#2" into the name would mint a separate venue — and demand a
    separate address and geocode — for every field at Kingsmere.
    """
    kingsmere = [e for e in by_uid.values() if e.venue and e.venue.name == "Kingsmere"]

    assert kingsmere, "no Kingsmere events"
    assert {e.venue.field for e in kingsmere} == {"#2"}
    assert all(e.venue.name == "Kingsmere" for e in kingsmere)


def test_a_summary_with_no_separator_still_yields_a_type():
    parsed = parse_summary("Practice")
    assert parsed.event_type == "Practice"
    assert parsed.venue_text is None
    assert parsed.opponent is None


def test_this_feed_names_venues_not_opponents(by_uid):
    """Team 758329's summaries carry a venue after the dash and never an
    opponent. Inventing one would put an unverifiable fixture in the title."""
    assert all(e.opponent is None for e in by_uid.values())
    assert all(e.home is None for e in by_uid.values())


def test_no_coordinates_or_address_are_invented(by_uid):
    for event in by_uid.values():
        if event.venue:
            assert event.venue.address is None
            assert not event.venue.resolved


# --- the other two coaches --------------------------------------------------
#
# Same platform, same season, entirely different habits. These are the cases a
# parser written against team 758329 alone gets silently wrong.

HAWKS_FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_otters_sample.ics"
COMETS_FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_wrens_sample.ics"

OTTERS = Activity(id="otters", child_id="kid", name="Otters", sport="soccer",
                 emoji="⚽️", tz="America/New_York")
WRENS = Activity(id="wrens", child_id="kid", name="Wrens", sport="soccer",
                  emoji="⚽️", tz="America/New_York")


@pytest.fixture(scope="module")
def otters():
    return parse_feed(HAWKS_FIXTURE.read_bytes(), OTTERS, source_id="tr-otters")


@pytest.fixture(scope="module")
def wrens():
    return parse_feed(COMETS_FIXTURE.read_bytes(), WRENS, source_id="tr-wrens")


def test_us_vs_them_summaries_are_games_despite_no_type_word(otters):
    """"Otters vs Chargers" contains neither "Game" nor "Practice".

    Classifying on type words alone files all twelve fixtures as practices.
    """
    assert sum(1 for e in otters.events if e.is_game) == 12
    assert sum(1 for e in otters.events if not e.is_game) == 8
    assert otters.unknown_types == []


def test_home_and_away_come_from_which_side_we_are_on(otters):
    by_opponent = {e.opponent: e for e in otters.events if e.opponent}

    assert by_opponent["Chargers"].home is True    # "Otters vs Chargers"
    assert by_opponent["Rampart"].home is False      # "Rampart vs Otters"
    assert sum(1 for e in otters.events if e.home is True) == 7
    assert sum(1 for e in otters.events if e.home is False) == 5
    assert "Otters" not in by_opponent, "named ourselves as the opponent"


def test_type_led_fixtures_yield_an_opponent_but_no_home_claim(wrens):
    """"Game vs Cougars" says who, not where — home must stay unknown."""
    by_opponent = {e.opponent: e for e in wrens.events if e.opponent}

    assert "Cougars" in by_opponent
    assert by_opponent["Cougars"].home is None
    assert all(e.home is None for e in wrens.events)
    assert sum(1 for e in wrens.events if e.is_game) == 11


def test_a_leading_type_word_is_not_mistaken_for_a_team(wrens):
    """"First Game vs Sentries" — the opponent is Sentries, not "First Game"."""
    by_opponent = {e.opponent: e for e in wrens.events if e.opponent}

    assert "Sentries" in by_opponent
    assert by_opponent["Sentries"].detail == "First Game"
    assert by_opponent["Sentries"].is_game is True


def test_location_field_is_preferred_over_the_summary_tail(wrens):
    """Teams that fill LOCATION get their venue from it, not from parsing."""
    venues = {e.venue.name for e in wrens.events if e.venue}

    assert "Larkspur Elementary School" in venues
    assert "Kingsmere Meadow Park" in venues


def test_description_is_carried_into_the_body(wrens):
    """One coach keeps a snack rota there; it is real information to a parent."""
    with_body = [e for e in wrens.events if e.body]

    assert with_body, "dropped the DESCRIPTION every event carries"
    assert any("has snacks" in e.body for e in with_body)


def test_an_unidentifiable_fixture_claims_no_opponent():
    """If neither side matches our team, naming either one is a coin flip."""
    stranger = Activity(id="x", child_id="c", name="Wanderers", sport="soccer",
                        emoji="⚽️", tz="America/New_York")
    result = parse_feed(HAWKS_FIXTURE.read_bytes(), stranger, source_id="x")

    assert all(e.opponent is None for e in result.events)
    assert result.unidentified, "silently dropped fixtures instead of reporting them"
    # Still recognisably fixtures, so they must not be filed as practices.
    assert sum(1 for e in result.events if e.is_game) == 0


@pytest.mark.parametrize(
    "summary, opponent, home",
    [
        ("Otters vs Chargers", "Chargers", True),
        ("Rampart vs Otters", "Rampart", False),
        ("Otters @ Meteors", "Meteors", True),
        ("Otters v. Badgers", "Badgers", True),
        ("Game vs Cougars", "Cougars", None),
        ("Practice at Kingsmere", None, None),   # "at" is a venue, not a fixture
    ],
)
def test_fixture_shapes(summary, opponent, home):
    parsed = parse_summary(summary, ("Otters",))
    assert parsed.opponent == opponent
    assert parsed.home is home


# --- change detection -------------------------------------------------------


def test_content_hash_ignores_modification_timestamps():
    """TeamReach publishes no SEQUENCE, and writes DTSTAMP == LAST-MODIFIED on
    every event — both are mtimes, so neither may reach the hash."""
    base = (
        "BEGIN:VEVENT\r\nUID:1@teamreach\r\nDTSTART:20260303T230000Z\r\n"
        "DTEND:20260304T000000Z\r\nSUMMARY:Game - Windmere\r\n"
        "DTSTAMP:{stamp}\r\nLAST-MODIFIED:{stamp}\r\nEND:VEVENT\r\n"
    )
    wrap = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\n{}END:VCALENDAR\r\n"
    from icalendar import Calendar

    def one(stamp):
        cal = Calendar.from_ical(wrap.format(base.format(stamp=stamp)))
        return content_hash([c for c in cal.walk() if c.name == "VEVENT"][0])

    assert one("20260226T225226Z") == one("20260830T101500Z")


def test_content_hash_reacts_to_a_real_change():
    from icalendar import Calendar

    wrap = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nBEGIN:VEVENT\r\n"
        "UID:1@teamreach\r\nDTSTART:20260303T230000Z\r\nDTEND:20260304T000000Z\r\n"
        "SUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    def one(summary):
        cal = Calendar.from_ical(wrap.format(summary=summary))
        return content_hash([c for c in cal.walk() if c.name == "VEVENT"][0])

    assert one("Game - Windmere") != one("Game - Kingsmere #2")


# --- guards -----------------------------------------------------------------


def test_empty_feed_is_not_evidence_of_cancellation():
    with pytest.raises(FeedError):
        parse_feed(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nEND:VCALENDAR\r\n",
            ACTIVITY, source_id="x",
        )


def test_garbage_feed_raises_rather_than_returning_nothing():
    with pytest.raises(FeedError):
        parse_feed(b"not a calendar at all", ACTIVITY, source_id="x")


def test_missing_dtend_defaults_to_zero_length_not_an_invented_duration(by_uid):
    """One event in the real feed has no DTEND."""
    event = by_uid["24611220@teamreach"]
    assert event.ends_at == event.starts_at


def test_missing_dtend_can_be_filled_by_deployment_choice():
    result = parse_feed(
        FIXTURE.read_bytes(), ACTIVITY, source_id="x", default_duration_min=60
    )
    event = {e.uid: e for e in result.events}["24611220@teamreach"]
    assert (event.ends_at - event.starts_at).total_seconds() == 3600


# --- venue vs field ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, name, field",
    [
        ("Kingsmere #2", "Kingsmere", "#2"),
        ("Kingsmere Meadow Park Soccer Fields", "Kingsmere Meadow Park Soccer Fields", None),
        ("Larkspur Elementary School", "Larkspur Elementary School", None),
        ("Copperfield Athletic Complex", "Copperfield Athletic Complex", None),
        ("McReynolds Athletic Complex Field 3", "McReynolds Athletic Complex", "Field 3"),
        ("Ashgrove High School Gym", "Ashgrove High School", "Gym"),
    ],
)
def test_split_field_keeps_plural_place_names_intact(raw, name, field):
    """"Soccer Fields" is part of what the place is called; "#2" is not."""
    from calsync.normalize.venue import split_field

    assert split_field(raw) == (name, field)


# --- an extensible vocabulary -----------------------------------------------


def test_a_taught_word_classifies_an_otherwise_unknown_type():
    """Coaches invent labels faster than an adapter can enumerate them."""
    assert classify("Scrimmage") is None
    assert classify("Scrimmage", game_words=["scrimmage"]) is True
    assert classify("Skills Session", practice_words=["skills session"]) is False


def test_operator_vocabulary_beats_the_built_in_heuristic():
    """A team whose "Game Prep" is a practice has to be able to say so.

    If the built-in `\\bgame\\b` were checked first there would be no way to
    correct it, and the setting would look honoured while doing nothing.
    """
    assert classify("Game Prep") is True
    assert classify("Game Prep", practice_words=["game prep"]) is False


def test_taught_words_extend_rather_than_replace():
    """Everything unlisted still falls through to the adapter's own words."""
    assert classify("Practice", game_words=["scrimmage"]) is False
    assert classify("Game", practice_words=["skills session"]) is True


def test_the_vocabulary_reaches_the_adapter_through_source_config():
    """`sources.config` was stored and never delivered to an adapter until now.

    Asserted end to end through `sources.parse`, because the gap was in the
    wiring rather than in `classify` — the vocabulary worked fine and nothing
    ever handed it over.
    """
    from calsync import sources

    raw = FIXTURE.read_bytes()
    plain = sources.parse("teamreach", raw, ACTIVITY, source_id="s")
    taught = sources.parse(
        "teamreach", raw, ACTIVITY, source_id="s",
        config={"practice_words": ["playoff game"]},
    )

    def games(result):
        return {e.uid for e in result.events if e.is_game}

    assert games(taught) < games(plain), (
        "teaching the source that a playoff game is a practice changed nothing, "
        "so sources.config still is not reaching the adapter"
    )
