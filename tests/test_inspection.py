"""Feed inspection, against the four real feeds.

These are the numbers in the docs/ONBOARDING.md §2 table. The table is a
measurement of what four coaches actually published, so it is the assertion:
if a change makes inspection propose something different for these feeds, the
change is wrong until the table is re-measured.

The interesting case is the one where inspection must propose *nothing*. Two of
the three TeamReach feeds give no evidence of our own team's name, and a
confident wrong answer there is worse than a blank field — the operator would
accept it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calsync.inspection import InspectionError, detect_kind, inspect_feed

FIXTURES = Path(__file__).parent / "fixtures"

HURRICANES = FIXTURES / "teamreach_sample.ics"
HAWKS = FIXTURES / "teamreach_hawks_sample.ics"
COMETS = FIXTURES / "teamreach_comets_sample.ics"
PLAYER360 = FIXTURES / "player360_sample.ics"


def read(path: Path):
    return inspect_feed(path.read_bytes())


@pytest.fixture(scope="module")
def hawks():
    return read(HAWKS)


@pytest.fixture(scope="module")
def comets():
    return read(COMETS)


@pytest.fixture(scope="module")
def hurricanes():
    return read(HURRICANES)


@pytest.fixture(scope="module")
def rush():
    return read(PLAYER360)


# --- the §2 table -----------------------------------------------------------


def test_team_name_comes_from_the_calendar_name(hawks, comets, hurricanes):
    assert hawks.calendar_name == "Hawks Spring 2026"
    assert comets.calendar_name == "Comets"
    assert hurricanes.calendar_name == "Inter HURRICANES"


def test_player360_publishes_a_generic_calendar_name(rush):
    """Marked "generic" in the table — the operator has to name that one."""
    assert rush.calendar_name == "360Player Event calendar"
    assert rush.team_token is None


def test_every_feed_yields_season_bounds(hawks, comets, hurricanes, rush):
    for inspection in (hawks, comets, hurricanes, rush):
        assert inspection.season_start is not None
        assert inspection.season_end is not None
        assert inspection.season_start <= inspection.season_end


def test_hawks_team_token_falls_out_of_frequency(hawks):
    """Our name is on every fixture; each opponent appears once or twice."""
    counts = {t.token: t.count for t in hawks.tokens}
    assert counts["Hawks"] == 12
    assert max(n for token, n in counts.items() if token != "Hawks") <= 2
    assert hawks.team_token == "Hawks"


def test_a_type_versus_opponent_feed_proposes_no_token(comets):
    """"Game vs Jaguars" names an opponent and says nothing about us.

    Every opponent appears exactly once, so the top candidate would be an
    arbitrary other team. Proposing it would look like an answer.
    """
    assert comets.team_token is None


def test_a_feed_with_no_fixtures_in_the_summary_proposes_no_token(hurricanes):
    """"Game - Riverview #2" is type-then-venue; no team name appears at all."""
    assert hurricanes.tokens == ()
    assert hurricanes.team_token is None


def test_every_feed_yields_venues(hawks, comets, hurricanes, rush):
    for inspection in (hawks, comets, hurricanes, rush):
        assert inspection.venues, "no venues derived"


def test_venues_exclude_the_field_designator(hurricanes):
    """"Riverview #2" is one park with one pin, not a venue per field."""
    names = [v.name for v in hurricanes.venues]
    assert "Riverview" in names
    assert not any("#" in name for name in names)

    riverview = next(v for v in hurricanes.venues if v.name == "Riverview")
    assert riverview.fields == ("#2",)


def test_venues_are_read_from_the_summary_when_there_is_no_location(hurricanes):
    """This feed sets no LOCATION at all — the venue is the summary tail."""
    assert any("no event sets LOCATION" in w for w in hurricanes.warnings)
    names = {v.name for v in hurricanes.venues}
    assert {"Riverview", "Passage", "Menchville"} <= names


def test_player360_venue_addresses_are_split_off(rush):
    wolf_trap = next(v for v in rush.venues if v.name == "Wolf Trap Park")
    assert wolf_trap.address == "1009 Wolf Trap Rd, Yorktown VA 23692"
    assert wolf_trap.count == 3


def test_venues_are_ordered_by_how_often_they_appear(comets):
    counts = [v.count for v in comets.venues]
    assert counts == sorted(counts, reverse=True)
    assert comets.venues[0].name == "Sanford Elementary School"


# --- counts -----------------------------------------------------------------


def test_counts_account_for_every_event(hawks, comets, hurricanes, rush):
    for inspection in (hawks, comets, hurricanes, rush):
        total = (
            inspection.reads_as_games
            + inspection.reads_as_practices
            + inspection.reads_as_unclear
        )
        assert total == inspection.event_count


def test_a_us_vs_them_feed_reads_its_fixtures_without_any_configuration(hawks):
    """A named opponent implies a fixture, which is this feed's only signal."""
    assert hawks.reads_as_games == 12
    assert hawks.has_fixtures


def test_player360_classifies_on_categories(rush):
    assert rush.reads_as_games == 2
    assert rush.reads_as_practices == 2


# --- adapter detection ------------------------------------------------------


def test_adapter_is_detected_from_prodid(hawks, rush):
    assert hawks.kind == "teamreach"
    assert rush.kind == "player360"


def test_an_unrecognised_producer_is_not_guessed_at():
    assert detect_kind("-//Some Other League//EN") is None
    assert detect_kind(None) is None


# --- refusing to inspect ----------------------------------------------------


def test_an_unparseable_body_is_refused():
    with pytest.raises(InspectionError):
        inspect_feed(b"this is not a calendar")


def test_an_empty_body_is_refused():
    with pytest.raises(InspectionError):
        inspect_feed(b"")


def test_a_valid_but_eventless_feed_is_refused():
    """Nothing to onboard, and zeros on the page would read as an answer."""
    with pytest.raises(InspectionError):
        inspect_feed(b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")


# --- purity -----------------------------------------------------------------


def test_inspection_is_reproducible(hawks):
    """Same bytes, same proposal — an unstable one would look like a change."""
    again = read(HAWKS)
    assert again == hawks


def test_the_raw_body_is_hashed(hawks):
    assert len(hawks.raw_sha256) == 64


def test_summaries_are_carried_back_as_evidence(comets):
    """The operator has to be able to see the convention, not just the verdict."""
    # "Practice" and "Practice " are the same convention typed twice; the
    # evidence panel should say 8, not split them.
    assert ("Practice", 8) in comets.summaries
    assert any(s.startswith("Game vs") for s, _ in comets.summaries)
