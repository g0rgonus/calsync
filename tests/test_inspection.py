"""Feed inspection, against the four real feeds.

These are the numbers inspection proposes for each feed. They are a
measurement of what four coaches actually published, so it is the assertion:
if a change makes inspection propose something different for these feeds, the
change is wrong until they are re-measured.

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

ALLIANCE = FIXTURES / "teamreach_sample.ics"
ROGUES = FIXTURES / "teamreach_rogues_sample.ics"
GUNGANS = FIXTURES / "teamreach_gungans_sample.ics"
PLAYER360 = FIXTURES / "player360_sample.ics"


def read(path: Path):
    return inspect_feed(path.read_bytes())


@pytest.fixture(scope="module")
def rogues():
    return read(ROGUES)


@pytest.fixture(scope="module")
def gungans():
    return read(GUNGANS)


@pytest.fixture(scope="module")
def alliance():
    return read(ALLIANCE)


@pytest.fixture(scope="module")
def jedi():
    return read(PLAYER360)


# --- the §2 table -----------------------------------------------------------


def test_team_name_comes_from_the_calendar_name(rogues, gungans, alliance):
    assert rogues.calendar_name == "Rogues Spring 2026"
    assert gungans.calendar_name == "Gungans"
    assert alliance.calendar_name == "Rebel ALLIANCE"


def test_player360_publishes_a_generic_calendar_name(jedi):
    """Marked "generic" in the table — the operator has to name that one."""
    assert jedi.calendar_name == "360Player Event calendar"


def test_a_league_match_proposes_the_team_and_not_the_whole_left_side(jedi):
    """"U10RS RSL Match vs Mos Eisley FC U10" names the team, league and type.

    Splitting on the separator alone took all three as our name, and two such
    fixtures clear the frequency bar — so a real Player360 feed proposed
    "U10RS RSL Match" as the team. `U10RS` is the part that is actually ours,
    and it is the token `strip_known_tokens` needs to turn "U10RS Practice"
    into "Practice".
    """
    assert jedi.team_token == "U10RS"
    assert all("Match" not in t.token for t in jedi.tokens)


def test_every_feed_yields_season_bounds(rogues, gungans, alliance, jedi):
    for inspection in (rogues, gungans, alliance, jedi):
        assert inspection.season_start is not None
        assert inspection.season_end is not None
        assert inspection.season_start <= inspection.season_end


def test_rancors_team_token_falls_out_of_frequency(rogues):
    """Our name is on every fixture; each opponent appears once or twice."""
    counts = {t.token: t.count for t in rogues.tokens}
    assert counts["Rogues"] == 12
    assert max(n for token, n in counts.items() if token != "Rogues") <= 2
    assert rogues.team_token == "Rogues"


def test_a_type_versus_opponent_feed_proposes_no_token(gungans):
    """"Game vs Krayts" names an opponent and says nothing about us.

    Every opponent appears exactly once, so the top candidate would be an
    arbitrary other team. Proposing it would look like an answer.
    """
    assert gungans.team_token is None


def test_a_feed_with_no_fixtures_in_the_summary_proposes_no_token(alliance):
    """"Game - Anchorhead #2" is type-then-venue; no team name appears at all."""
    assert alliance.tokens == ()
    assert alliance.team_token is None


def test_every_feed_yields_venues(rogues, gungans, alliance, jedi):
    for inspection in (rogues, gungans, alliance, jedi):
        assert inspection.venues, "no venues derived"


def test_venues_exclude_the_field_designator(alliance):
    """"Anchorhead #2" is one park with one pin, not a venue per field."""
    names = [v.name for v in alliance.venues]
    assert "Anchorhead" in names
    assert not any("#" in name for name in names)

    anchorhead = next(v for v in alliance.venues if v.name == "Anchorhead")
    assert anchorhead.fields == ("#2",)


def test_venues_are_read_from_the_summary_when_there_is_no_location(alliance):
    """This feed sets no LOCATION at all — the venue is the summary tail."""
    assert any("no event sets LOCATION" in w for w in alliance.warnings)
    names = {v.name for v in alliance.venues}
    assert {"Anchorhead", "Dagobah", "Endor"} <= names


def test_player360_venue_addresses_are_split_off(jedi):
    theed = next(v for v in jedi.venues if v.name == "Theed Park")
    assert theed.address == "1009 Theed Rd, Naboo NX 40114"
    assert theed.count == 3


def test_venues_are_ordered_by_how_often_they_appear(gungans):
    counts = [v.count for v in gungans.venues]
    assert counts == sorted(counts, reverse=True)
    assert gungans.venues[0].name == "Lars Homestead Elementary School"


# --- counts -----------------------------------------------------------------


def test_counts_account_for_every_event(rogues, gungans, alliance, jedi):
    for inspection in (rogues, gungans, alliance, jedi):
        total = (
            inspection.reads_as_games
            + inspection.reads_as_practices
            + inspection.reads_as_unclear
        )
        assert total == inspection.event_count


def test_a_us_vs_them_feed_reads_its_fixtures_without_any_configuration(rogues):
    """A named opponent implies a fixture, which is this feed's only signal."""
    assert rogues.reads_as_games == 12
    assert rogues.has_fixtures


def test_player360_classifies_on_categories(jedi):
    # Two league matches and the festival; CATEGORIES is the whole rule here.
    assert jedi.reads_as_games == 3
    assert jedi.reads_as_practices == 2


# --- adapter detection ------------------------------------------------------


def test_adapter_is_detected_from_prodid(rogues, jedi):
    assert rogues.kind == "teamreach"
    assert jedi.kind == "player360"


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


def test_inspection_is_reproducible(rogues):
    """Same bytes, same proposal — an unstable one would look like a change."""
    again = read(ROGUES)
    assert again == rogues


def test_the_raw_body_is_hashed(rogues):
    assert len(rogues.raw_sha256) == 64


def test_summaries_are_carried_back_as_evidence(gungans):
    """The operator has to be able to see the convention, not just the verdict."""
    # "Practice" and "Practice " are the same convention typed twice; the
    # evidence panel should say 8, not split them.
    assert ("Practice", 8) in gungans.summaries
    assert any(s.startswith("Game vs") for s, _ in gungans.summaries)
