"""Golden tests against the real TeamReach payload.

Every assertion encodes a finding (docs/connectors/teamreach.md). The feed is
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
    name="Inter Alliance",
    sport="soccer",
    emoji="⚽️",
    tz="America/New_York",
)


@pytest.fixture(scope="module")
def result():
    return parse_feed(FIXTURE.read_bytes(), ACTIVITY, source_id="tr-alliance")


@pytest.fixture(scope="module")
def by_uid(result):
    return {e.uid: e for e in result.events}


# --- structure --------------------------------------------------------------


def test_all_events_parsed(result):
    assert len(result.events) == 23
    assert result.raw_sha256


def test_uid_is_stable_and_passed_through(by_uid):
    """<digits>@teamreach, unique per event — no generation timestamp.

    This is the good case.
    """
    assert len(by_uid) == 23
    assert all(uid.endswith("@teamreach") for uid in by_uid)
    assert "61253410@teamreach" in by_uid


def test_registry_dispatches_by_kind():
    result = parse(
        "teamreach", FIXTURE.read_bytes(), ACTIVITY, source_id="tr-alliance"
    )
    assert len(result.events) == 23


def test_times_are_utc(by_uid):
    event = by_uid["61253410@teamreach"]
    assert event.starts_at == datetime(2026, 3, 3, 23, 0, tzinfo=timezone.utc)
    assert all(e.starts_at.tzinfo is not None for e in by_uid.values())


# --- game vs practice, from the summary alone -------------------------------


def test_practice_and_game_are_distinguished_without_categories(by_uid):
    assert by_uid["61253410@teamreach"].is_game is False   # "Practice - Dagobah "
    assert by_uid["61610930@teamreach"].is_game is True    # "Game - Anchorhead #2"


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
        "DTEND:20260304T000000Z\r\nSUMMARY:Team Photo - Dagobah\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    result = parse_feed(ics, ACTIVITY, source_id="x")

    assert result.events[0].is_game is False
    assert result.unknown_types == ["Team Photo"]


# --- the coach-typed summary ------------------------------------------------


@pytest.mark.parametrize(
    "summary, event_type, venue",
    [
        ("Practice - Dagobah ", "Practice", "Dagobah"),
        ("Practice  - Dagobah ", "Practice", "Dagobah"),      # doubled space
        ("Practice- Dagobah", "Practice", "Dagobah"),          # no leading space
        ("Game - Anchorhead#2", "Game", "Anchorhead #2"),        # missing # space
        ("Playoff Game - Dagobah.  6pm", "Playoff Game", "Dagobah"),
        ("Make Up Game- Dagobah ", "Make Up Game", "Dagobah"),
    ],
)
def test_summary_variants_normalize_to_the_same_fields(summary, event_type, venue):
    parsed = parse_summary(summary)
    assert parsed.event_type == event_type
    assert clean_venue(parsed.venue_text) == venue


def test_venue_variants_collapse_to_one_name(by_uid):
    """"Anchorhead #2" and "Anchorhead#2" are the same place in the same feed."""
    venues = {e.venue.name for e in by_uid.values() if e.venue}
    assert venues == {"Dagobah", "Endor", "Anchorhead"}


def test_field_designator_is_split_off_the_venue(by_uid):
    """One park, one pin, many fields.

    Folding "#2" into the name would mint a separate venue — and demand a
    separate address and geocode — for every field at Anchorhead.
    """
    anchorhead = [e for e in by_uid.values() if e.venue and e.venue.name == "Anchorhead"]

    assert anchorhead, "no Anchorhead events"
    assert {e.venue.field for e in anchorhead} == {"#2"}
    assert all(e.venue.name == "Anchorhead" for e in anchorhead)


def test_a_summary_with_no_separator_still_yields_a_type():
    parsed = parse_summary("Practice")
    assert parsed.event_type == "Practice"
    assert parsed.venue_text is None
    assert parsed.opponent is None


def test_this_feed_names_venues_not_opponents(by_uid):
    """Team 900417's summaries carry a venue after the dash and never an
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
# parser written against team 900417 alone gets silently wrong.

RANCORS_FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_rogues_sample.ics"
COMETS_FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_gungans_sample.ics"

ROGUES = Activity(id="rogues", child_id="kid", name="Rogues", sport="soccer",
                 emoji="⚽️", tz="America/New_York")
GUNGANS = Activity(id="gungans", child_id="kid", name="Gungans", sport="soccer",
                  emoji="⚽️", tz="America/New_York")


@pytest.fixture(scope="module")
def rogues():
    return parse_feed(RANCORS_FIXTURE.read_bytes(), ROGUES, source_id="tr-rogues")


@pytest.fixture(scope="module")
def gungans():
    return parse_feed(COMETS_FIXTURE.read_bytes(), GUNGANS, source_id="tr-gungans")


def test_us_vs_them_summaries_are_games_despite_no_type_word(rogues):
    """"Rogues vs Tuskens" contains neither "Game" nor "Practice".

    Classifying on type words alone files all twelve fixtures as practices.
    """
    assert sum(1 for e in rogues.events if e.is_game) == 12
    assert sum(1 for e in rogues.events if not e.is_game) == 8
    assert rogues.unknown_types == []


def test_home_and_away_come_from_which_side_we_are_on(rogues):
    by_opponent = {e.opponent: e for e in rogues.events if e.opponent}

    assert by_opponent["Tuskens"].home is True    # "Rogues vs Tuskens"
    assert by_opponent["Scarif"].home is False      # "Scarif vs Rogues"
    assert sum(1 for e in rogues.events if e.home is True) == 7
    assert sum(1 for e in rogues.events if e.home is False) == 5
    assert "Rogues" not in by_opponent, "named ourselves as the opponent"


def test_type_led_fixtures_yield_an_opponent_but_no_home_claim(gungans):
    """"Game vs Krayts" says who, not where — home must stay unknown."""
    by_opponent = {e.opponent: e for e in gungans.events if e.opponent}

    assert "Krayts" in by_opponent
    assert by_opponent["Krayts"].home is None
    assert all(e.home is None for e in gungans.events)
    assert sum(1 for e in gungans.events if e.is_game) == 11


def test_a_leading_type_word_is_not_mistaken_for_a_team(gungans):
    """"First Game vs Stormtroopers" — the opponent is Stormtroopers, not "First Game"."""
    by_opponent = {e.opponent: e for e in gungans.events if e.opponent}

    assert "Stormtroopers" in by_opponent
    assert by_opponent["Stormtroopers"].detail == "First Game"
    assert by_opponent["Stormtroopers"].is_game is True


def test_location_field_is_preferred_over_the_summary_tail(gungans):
    """Teams that fill LOCATION get their venue from it, not from parsing."""
    venues = {e.venue.name for e in gungans.events if e.venue}

    assert "Lars Homestead Elementary School" in venues
    assert "Anchorhead Meadow Park" in venues


def test_description_is_carried_into_the_body(gungans):
    """One coach keeps a snack rota there; it is real information to a parent."""
    with_body = [e for e in gungans.events if e.body]

    assert with_body, "dropped the DESCRIPTION every event carries"
    assert any("has snacks" in e.body for e in with_body)


def test_an_unidentifiable_fixture_claims_no_opponent():
    """If neither side matches our team, naming either one is a coin flip."""
    stranger = Activity(id="x", child_id="c", name="Banthas", sport="soccer",
                        emoji="⚽️", tz="America/New_York")
    result = parse_feed(RANCORS_FIXTURE.read_bytes(), stranger, source_id="x")

    assert all(e.opponent is None for e in result.events)
    assert result.unidentified, "silently dropped fixtures instead of reporting them"
    # Still recognisably fixtures, so they must not be filed as practices.
    assert sum(1 for e in result.events if e.is_game) == 0


@pytest.mark.parametrize(
    "summary, opponent, home",
    [
        ("Rogues vs Tuskens", "Tuskens", True),
        ("Scarif vs Rogues", "Scarif", False),
        ("Rogues @ Wookiees", "Wookiees", True),
        ("Rogues v. Dewbacks", "Dewbacks", True),
        ("Game vs Krayts", "Krayts", None),
        ("Practice at Anchorhead", None, None),   # "at" is a venue, not a fixture
    ],
)
def test_fixture_shapes(summary, opponent, home):
    parsed = parse_summary(summary, ("Rogues",))
    assert parsed.opponent == opponent
    assert parsed.home is home


# --- change detection -------------------------------------------------------


def test_content_hash_ignores_modification_timestamps():
    """TeamReach publishes no SEQUENCE, and writes DTSTAMP == LAST-MODIFIED on
    every event — both are mtimes, so neither may reach the hash."""
    base = (
        "BEGIN:VEVENT\r\nUID:1@teamreach\r\nDTSTART:20260303T230000Z\r\n"
        "DTEND:20260304T000000Z\r\nSUMMARY:Game - Dagobah\r\n"
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

    assert one("Game - Dagobah") != one("Game - Anchorhead #2")


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
    event = by_uid["61611220@teamreach"]
    assert event.ends_at == event.starts_at


def test_missing_dtend_can_be_filled_by_deployment_choice():
    result = parse_feed(
        FIXTURE.read_bytes(), ACTIVITY, source_id="x", default_duration_min=60
    )
    event = {e.uid: e for e in result.events}["61611220@teamreach"]
    assert (event.ends_at - event.starts_at).total_seconds() == 3600


# --- venue vs field ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, name, field",
    [
        ("Anchorhead #2", "Anchorhead", "#2"),
        ("Anchorhead Meadow Park Soccer Fields", "Anchorhead Meadow Park Soccer Fields", None),
        ("Lars Homestead Elementary School", "Lars Homestead Elementary School", None),
        ("Mos Eisley Athletic Complex", "Mos Eisley Athletic Complex", None),
        ("McReynolds Athletic Complex Field 3", "McReynolds Athletic Complex", "Field 3"),
        ("Endor High School Gym", "Endor High School", "Gym"),
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


# --- a day with no time on it -----------------------------------------------
#
# Observed on a real feed 2026-08-28: two `DTSTART;VALUE=DATE` events —
# "Semifinal Games" and "Final Tournament Game" — entered months before anyone
# knew the kick-off. The adapter raised on them, which killed all 21 events in
# the feed over the 2 it could not read.

ALLDAY = (Path(__file__).parent / "fixtures" / "teamreach_allday_sample.ics").read_bytes()


def test_a_day_with_no_time_does_not_kill_the_feed():
    """The failure this fixes: two unreadable events took the other nineteen
    with them, so a live season never reached the calendar at all."""
    result = parse_feed(ALLDAY, ACTIVITY, source_id="tr-ewoks")

    assert len(result.events) == 3, "an all-day event stopped the parse"
    assert sum(e.all_day for e in result.events) == 2


def test_an_all_day_event_is_anchored_at_local_midnight():
    """Not UTC midnight. Anchoring in UTC puts the event on the previous day for
    anyone west of Greenwich — a semifinal on the 16th, shown on the 15th."""
    from zoneinfo import ZoneInfo

    events = {e.uid: e for e in parse_feed(
        ALLDAY, ACTIVITY, source_id="tr-ewoks").events}
    semi = events["31000002@teamreach"]

    assert semi.all_day
    local = semi.starts_at.astimezone(ZoneInfo(ACTIVITY.tz))
    assert (local.year, local.month, local.day) == (2026, 10, 17)
    assert (local.hour, local.minute) == (0, 0)
    # DTEND on a DATE is exclusive: the 17th to the 18th is one day.
    assert (semi.ends_at - semi.starts_at).days == 1


def test_a_timed_event_in_the_same_feed_is_untouched():
    events = {e.uid: e for e in parse_feed(
        ALLDAY, ACTIVITY, source_id="tr-ewoks").events}
    timed = events["31000001@teamreach"]

    assert not timed.all_day
    assert timed.starts_at.isoformat() == "2026-09-08T23:00:00+00:00"


def test_an_unreadable_event_names_itself_and_the_reason():
    """"missing UID or DTSTART" sent somebody to the logs for a feed whose UID
    and DTSTART were both present."""
    broken = ALLDAY.replace(b"DTSTART;VALUE=DATE:20261017",
                            b"DTSTART;VALUE=DURATION:PT1H")
    with pytest.raises(FeedError) as excinfo:
        parse_feed(broken, ACTIVITY, source_id="tr-ewoks")

    message = str(excinfo.value)
    assert "31000002@teamreach" in message, "which event?"
    assert "neither a timestamp nor a date" in message, "what is wrong with it?"
    assert "missing UID" not in message


# --- a third convention: the summary names only them ------------------------
#
# Observed 2026-08-28. A coach writing from the team's own point of view —
# "vs. Kamino B", "@ Hoth Classical" — so there is no left-hand side to
# match ourselves against. `_VERSUS` requires whitespace *before* the marker, so
# every one of these fell through to "a bare label": all eighteen fixtures in a
# live feed filed as practices, each reported as an unrecognised type.


def test_a_summary_that_names_only_the_opponent_is_a_fixture():
    """The failure this fixes: a season of games filed as practices."""
    for summary, opponent in (
        ("vs. Kamino B", "Kamino B"),
        ("vs Dagobah Prep", "Dagobah Prep"),
        ("@ Hoth Classical", "Hoth Classical"),
        ("v. Bespin Academy", "Bespin Academy"),
    ):
        parsed = parse_summary(summary, tokens=("ROGU",))
        assert parsed.opponent == opponent, summary
        assert classify(parsed.event_type, has_opponent=bool(parsed.opponent)) is True
        assert not parsed.unidentified


def test_a_leading_at_states_away_and_a_leading_vs_states_nothing():
    """"@" means at their place, which is positively known. "vs." only names
    the opponent — some feeds phrase every fixture that way, so claiming home
    from it would be the guess `normalize/title.py` refuses to make."""
    assert parse_summary("@ Hoth Classical").home is False
    assert parse_summary("vs. Kamino B").home is None


def test_the_at_sign_does_not_need_a_space_after_it():
    """One feed writes "@Endor Academy"."""
    parsed = parse_summary("@Endor Academy")
    assert parsed.opponent == "Endor Academy"
    assert parsed.home is False


def test_a_trailing_asterisk_does_not_mint_a_second_opponent():
    """One feed marks some fixtures with one and records nowhere what it means.
    Kept, it makes "Bespin Academy" and "Bespin Academy*" two different schools."""
    assert parse_summary("@Bespin Academy*").opponent == "Bespin Academy"
    assert parse_summary("vs. Bespin Academy").opponent == "Bespin Academy"


def test_a_team_whose_name_starts_with_vs_is_not_eaten():
    """"vs" needs a dot or a space after it, or the shape swallows a word."""
    parsed = parse_summary("Ryloth Juniors vs Naboo", tokens=("Ryloth Juniors",))
    assert parsed.opponent == "Naboo"
    assert parsed.home is True


def test_the_other_two_conventions_still_parse():
    """This shape is checked first, so it must not intercept the others."""
    both_named = parse_summary("Rogues vs Tuskens", tokens=("Rogues",))
    assert (both_named.opponent, both_named.home) == ("Tuskens", True)

    type_prefixed = parse_summary("Game vs Krayts")
    assert (type_prefixed.event_type, type_prefixed.opponent) == ("Game", "Krayts")

    bare = parse_summary("First Practice")
    assert bare.opponent is None
    assert classify(bare.event_type) is False


def test_an_opponent_only_feed_parses_end_to_end():
    """Through `parse_feed`, not just the string parser."""
    result = parse_feed(ALLDAY, ACTIVITY, source_id="tr-ewoks")
    fixture = {e.uid: e for e in result.events}["31000001@teamreach"]

    assert fixture.is_game
    assert fixture.opponent == "Coruscant Athletic"
    assert result.diagnostics.get("unidentified", []) == []
