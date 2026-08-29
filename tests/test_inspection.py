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

TEMPEST = FIXTURES / "teamreach_sample.ics"
OTTERS = FIXTURES / "teamreach_otters_sample.ics"
WRENS = FIXTURES / "teamreach_wrens_sample.ics"
PLAYER360 = FIXTURES / "player360_sample.ics"


def read(path: Path):
    return inspect_feed(path.read_bytes())


@pytest.fixture(scope="module")
def otters():
    return read(OTTERS)


@pytest.fixture(scope="module")
def wrens():
    return read(WRENS)


@pytest.fixture(scope="module")
def tempest():
    return read(TEMPEST)


@pytest.fixture(scope="module")
def vanguard():
    return read(PLAYER360)


# --- the §2 table -----------------------------------------------------------


def test_team_name_comes_from_the_calendar_name(otters, wrens, tempest):
    assert otters.calendar_name == "Otters Spring 2026"
    assert wrens.calendar_name == "Wrens"
    assert tempest.calendar_name == "Inter TEMPEST"


def test_player360_publishes_a_generic_calendar_name(vanguard):
    """Marked "generic" in the table — the operator has to name that one."""
    assert vanguard.calendar_name == "360Player Event calendar"


def test_a_league_match_proposes_the_team_and_not_the_whole_left_side(vanguard):
    """"U10PL PSL Match vs Harbour FC U10" names the team, league and type.

    Splitting on the separator alone took all three as our name, and two such
    fixtures clear the frequency bar — so a real Player360 feed proposed
    "U10PL PSL Match" as the team. `U10PL` is the part that is actually ours,
    and it is the token `strip_known_tokens` needs to turn "U10PL Practice"
    into "Practice".
    """
    assert vanguard.team_token == "U10PL"
    assert all("Match" not in t.token for t in vanguard.tokens)


def test_every_feed_yields_season_bounds(otters, wrens, tempest, vanguard):
    for inspection in (otters, wrens, tempest, vanguard):
        assert inspection.season_start is not None
        assert inspection.season_end is not None
        assert inspection.season_start <= inspection.season_end


def test_hawks_team_token_falls_out_of_frequency(otters):
    """Our name is on every fixture; each opponent appears once or twice."""
    counts = {t.token: t.count for t in otters.tokens}
    assert counts["Otters"] == 12
    assert max(n for token, n in counts.items() if token != "Otters") <= 2
    assert otters.team_token == "Otters"


def test_a_type_versus_opponent_feed_proposes_no_token(wrens):
    """"Game vs Cougars" names an opponent and says nothing about us.

    Every opponent appears exactly once, so the top candidate would be an
    arbitrary other team. Proposing it would look like an answer.
    """
    assert wrens.team_token is None


def test_a_feed_with_no_fixtures_in_the_summary_proposes_no_token(tempest):
    """"Game - Kingsmere #2" is type-then-venue; no team name appears at all."""
    assert tempest.tokens == ()
    assert tempest.team_token is None


def test_every_feed_yields_venues(otters, wrens, tempest, vanguard):
    for inspection in (otters, wrens, tempest, vanguard):
        assert inspection.venues, "no venues derived"


def test_venues_exclude_the_field_designator(tempest):
    """"Kingsmere #2" is one park with one pin, not a venue per field."""
    names = [v.name for v in tempest.venues]
    assert "Kingsmere" in names
    assert not any("#" in name for name in names)

    kingsmere = next(v for v in tempest.venues if v.name == "Kingsmere")
    assert kingsmere.fields == ("#2",)


def test_venues_are_read_from_the_summary_when_there_is_no_location(tempest):
    """This feed sets no LOCATION at all — the venue is the summary tail."""
    assert any("no event sets LOCATION" in w for w in tempest.warnings)
    names = {v.name for v in tempest.venues}
    assert {"Kingsmere", "Windmere", "Ashgrove"} <= names


def test_player360_venue_addresses_are_split_off(vanguard):
    thistledown = next(v for v in vanguard.venues if v.name == "Thistledown Park")
    assert thistledown.address == "1009 Thistledown Rd, Marbury NX 40114"
    assert thistledown.count == 3


def test_venues_are_ordered_by_how_often_they_appear(wrens):
    counts = [v.count for v in wrens.venues]
    assert counts == sorted(counts, reverse=True)
    assert wrens.venues[0].name == "Larkspur Elementary School"


# --- counts -----------------------------------------------------------------


def test_counts_account_for_every_event(otters, wrens, tempest, vanguard):
    for inspection in (otters, wrens, tempest, vanguard):
        total = (
            inspection.reads_as_games
            + inspection.reads_as_practices
            + inspection.reads_as_unclear
        )
        assert total == inspection.event_count


def test_a_us_vs_them_feed_reads_its_fixtures_without_any_configuration(otters):
    """A named opponent implies a fixture, which is this feed's only signal."""
    assert otters.reads_as_games == 12
    assert otters.has_fixtures


def test_player360_classifies_on_categories(vanguard):
    # Two league matches and the festival; CATEGORIES is the whole rule here.
    assert vanguard.reads_as_games == 3
    assert vanguard.reads_as_practices == 2


# --- adapter detection ------------------------------------------------------


def test_adapter_is_detected_from_prodid(otters, vanguard):
    assert otters.kind == "teamreach"
    assert vanguard.kind == "player360"


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


def test_inspection_is_reproducible(otters):
    """Same bytes, same proposal — an unstable one would look like a change."""
    again = read(OTTERS)
    assert again == otters


def test_the_raw_body_is_hashed(otters):
    assert len(otters.raw_sha256) == 64


def test_summaries_are_carried_back_as_evidence(wrens):
    """The operator has to be able to see the convention, not just the verdict."""
    # "Practice" and "Practice " are the same convention typed twice; the
    # evidence panel should say 8, not split them.
    assert ("Practice", 8) in wrens.summaries
    assert any(s.startswith("Game vs") for s, _ in wrens.summaries)
