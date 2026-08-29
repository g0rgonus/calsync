"""Warm-ups: the event no feed publishes.

A coach says "be there 45 minutes early" once at the start of a season and it
never appears in an export, so calsync synthesizes it from one number per team.
That makes these events unlike everything else in the pipeline — derived rather
than read — and the tests that matter are the ones about what that changes:
they must not vote in the disappearance guard, and switching them off must be
an ordinary cancellation rather than a held sync.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from calsync import db, repo, warmup
from calsync.diff import diff_poll
from calsync.models import Event, Venue
from calsync.render import build_body, render
from calsync.settings import Settings
from calsync.sync import sync_source
from calsync.targets import build

FIXTURE = Path(__file__).parent / "fixtures" / "player360_sample.ics"
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
EASTERN = ZoneInfo("America/New_York")


def _game(uid="g1", *, is_game=True, all_day=False, hour=14, **kwargs):
    start = datetime(2026, 8, 15, hour, 0, tzinfo=EASTERN)
    return Event(
        uid=uid,
        activity_id="jesse-soccer-vanguard",
        starts_at=start,
        ends_at=start + timedelta(hours=1, minutes=30),
        is_game=is_game,
        all_day=all_day,
        tz="America/New_York",
        venue=Venue(raw="Kingsmere #2", name="Kingsmere", address="9 Wren Rd", field="#2"),
        opponent="Chargers",
        source_id="p360-jesse-vanguard",
        content_hash="abc123",
        **kwargs,
    )


# --- what gets one ----------------------------------------------------------


def test_zero_minutes_changes_nothing():
    events = [_game()]
    assert warmup.expand(events, minutes=0) == events


def test_a_game_gets_a_warm_up_ending_at_kick_off():
    game = _game()
    expanded = warmup.expand([game], minutes=45)

    assert len(expanded) == 2
    warm = expanded[1]
    assert warm.warmup_for == game.uid
    assert warm.ends_at == game.starts_at
    assert warm.starts_at == game.starts_at - timedelta(minutes=45)
    # The single field that files it with the practices rather than the games.
    assert warm.is_game is False
    assert warm.venue == game.venue


def test_a_practice_gets_nothing():
    assert len(warmup.expand([_game(is_game=False)], minutes=45)) == 1


def test_an_all_day_game_gets_nothing():
    """45 minutes before local midnight is 23:15 the evening before, for a
    kick-off nobody has published — the same reason an all-day event gets no
    alarm."""
    assert len(warmup.expand([_game(all_day=True)], minutes=45)) == 1


def test_the_game_learns_its_own_arrival_time():
    game = _game()
    warmup.expand([game], minutes=45)
    assert game.arrive_at == game.starts_at - timedelta(minutes=45)


def test_a_held_game_holds_its_warm_up_with_it():
    """Otherwise the game waits in `enrichment` while its warm-up sits on the
    family's real calendar, in front of a fixture nobody can see."""
    game = _game(unresolved=("unidentified",))
    warm = warmup.expand([game], minutes=45)[1]
    assert warm.unresolved == ("unidentified",)
    assert warm.needs_enrichment


# --- identity ---------------------------------------------------------------


def test_the_uid_is_stable_across_polls():
    first = warmup.expand([_game()], minutes=45)[1]
    second = warmup.expand([_game()], minutes=45)[1]
    assert first.uid == second.uid
    assert warmup.is_synthetic(first.uid)


def test_a_feed_uid_is_not_mistaken_for_a_synthetic_one():
    assert not warmup.is_synthetic("360Player-event-4716716")


def test_changing_the_offset_changes_the_content_hash():
    """Otherwise the diff calls it unchanged and the warm-up never moves."""
    at45 = warmup.expand([_game()], minutes=45)[1]
    at30 = warmup.expand([_game()], minutes=30)[1]
    assert at45.uid == at30.uid
    assert at45.content_hash != at30.content_hash


def test_moving_the_game_moves_the_warm_up():
    moved = _game()
    moved.content_hash = "different"
    assert (
        warmup.expand([_game()], minutes=45)[1].content_hash
        != warmup.expand([moved], minutes=45)[1].content_hash
    )


# --- the guard --------------------------------------------------------------


def test_warm_ups_do_not_vote_in_the_disappearance_guard():
    """The threshold was measured against real cancellations. Counting a game's
    shadow as a second disappearance would trip it at two games instead of four.
    """
    # Twenty games, three cancelled: 15% and a count of 3, inside both
    # thresholds. Counting each one's warm-up as a second disappearance leaves
    # the percentage identical and doubles the count to 6, which is over.
    games = [_game(f"g{i}") for i in range(20)]
    tracked = {e.uid: e.content_hash for e in warmup.expand(games, minutes=45)}
    survivors = warmup.expand(games[3:], minutes=45)


    counted = diff_poll(
        survivors, tracked, now=NOW,
        counts_as_evidence=lambda uid: not warmup.is_synthetic(uid),
    )
    assert counted.anomaly is None
    assert len(counted.cancelled) == 6      # three games *and* their warm-ups

    # The same poll with everything voting is exactly what must not happen.
    assert diff_poll(survivors, tracked, now=NOW).is_anomalous


def test_switching_warm_ups_off_is_not_an_anomaly():
    """A season's worth of synthetic events vanishing at once is the shape the
    guard exists to refuse, and the one case where it is meant."""
    games = [_game(f"g{i}") for i in range(10)]
    tracked = {e.uid: e.content_hash for e in warmup.expand(games, minutes=45)}

    delta = diff_poll(
        games, tracked, now=NOW,
        counts_as_evidence=lambda uid: not warmup.is_synthetic(uid),
    )
    assert delta.anomaly is None
    assert len(delta.cancelled) == 10
    assert all(warmup.is_synthetic(uid) for uid in delta.cancelled)


def test_a_real_mass_disappearance_still_holds():
    """The exemption narrows what is counted; it must not weaken the guard."""
    games = [_game(f"g{i}") for i in range(10)]
    tracked = {e.uid: e.content_hash for e in warmup.expand(games, minutes=45)}

    delta = diff_poll(
        warmup.expand(games[:2], minutes=45), tracked, now=NOW,
        counts_as_evidence=lambda uid: not warmup.is_synthetic(uid),
    )
    assert delta.is_anomalous
    assert delta.anomaly_kind == "disappearance"
    assert delta.cancelled == []


# --- how it reads -----------------------------------------------------------


def _settings(conn):
    return Settings.load(conn)


def test_the_warm_up_takes_the_game_alarm_not_the_practice_one():
    """It is the thing you leave the house for, so the reminder that matters is
    the travel-time one, timed off the warm-up."""
    warm = warmup.expand([_game()], minutes=45)[1]
    assert warm.is_game is False
    assert warm.alarms_as_game is True
    assert _game(is_game=False).alarms_as_game is False


def test_the_body_says_when_the_game_starts(conn):
    settings = _settings(conn)
    activity = repo.get_activity(conn, "jesse-soccer-vanguard")
    warm = warmup.expand([_game()], minutes=45)[1]

    body = build_body(warm, activity, settings)
    assert "Start 13:15" in body
    assert "Game starts 14:00" in body


def test_the_game_body_states_the_arrival_time(conn):
    settings = _settings(conn)
    activity = repo.get_activity(conn, "jesse-soccer-vanguard")
    game = _game()
    warmup.expand([game], minutes=45)
    assert "Arrive: 13:15" in build_body(game, activity, settings)


def test_a_warm_up_uses_its_own_title_template(conn):
    from calsync.normalize import title as title_norm

    settings = _settings(conn)
    activity = repo.get_activity(conn, "jesse-soccer-vanguard")
    kids = [repo.get_child(conn, "jesse")]
    game = _game()
    warm = warmup.expand([game], minutes=45)[1]

    assert title_norm.render(game, activity, kids, settings) == "Jesse ⚽️ vs Chargers"
    assert (title_norm.render(warm, activity, kids, settings)
            == "Jesse ⚽️ vs Chargers warm-up")


def test_a_blank_warm_up_template_falls_back_rather_than_rendering_nothing(conn):
    from calsync.normalize import title as title_norm

    conn.execute(
        "UPDATE settings SET value = '' WHERE key = 'warmup_title_template'")
    conn.commit()

    settings = _settings(conn)
    activity = repo.get_activity(conn, "jesse-soccer-vanguard")
    kids = [repo.get_child(conn, "jesse")]
    warm = warmup.expand([_game()], minutes=45)[1]

    assert title_norm.render(warm, activity, kids, settings) == "Jesse ⚽️ vs Chargers"


def test_the_warm_up_is_routed_to_the_practice_calendar(conn):
    settings = _settings(conn)
    activity = repo.get_activity(conn, "jesse-soccer-vanguard")
    kids = [repo.get_child(conn, "jesse")]
    game = _game()
    warm = warmup.expand([game], minutes=45)[1]

    assert render(game, activity, kids, settings).collection == "games"
    assert render(warm, activity, kids, settings).collection == "practices"


# --- end to end -------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('jesse', 'Jesse', 'J', 1);
        INSERT INTO activities (id, child_id, name, sport_id, official_name,
                                league, age_group, tz, alarm_game_min,
                                alarm_practice_min, warmup_minutes)
             VALUES ('jesse-soccer-vanguard', 'jesse', 'Vanguard', 'soccer', 'U10PL',
                     'PSL', 'U10', 'America/New_York', 90, 30, 45);
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('p360-jesse-vanguard', 'jesse-soccer-vanguard', 'player360', 'feed');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def target(tmp_path):
    return build("ics_file", directory=tmp_path / "out")


def _sync(conn, target, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("raw", FIXTURE.read_bytes())
    return sync_source(conn, repo.list_sources(conn)[0], target, **kwargs)


def _set_warmup(conn, minutes):
    conn.execute("UPDATE activities SET warmup_minutes = ?", (minutes,))
    conn.commit()


def test_a_warm_up_is_written_for_every_game(conn, target, tmp_path):
    report = _sync(conn, target)
    assert report.status == "ok"

    written = {p.stem for p in (tmp_path / "out").rglob("*.ics")}
    warm = {uid for uid in written if warmup.is_synthetic(uid)}
    assert warm, "the feed has games, so it should have produced warm-ups"
    # One each, and every one points at a game that was actually written.
    assert all(uid.removeprefix(warmup.PREFIX) in written for uid in warm)
    assert len(warm) == report.fixtures_seen


def test_warm_ups_land_beside_the_practices(conn, target, tmp_path):
    _sync(conn, target)
    for path in (tmp_path / "out").rglob("*.ics"):
        if warmup.is_synthetic(path.stem):
            assert path.parent.name == "practices"


def test_the_second_poll_writes_nothing(conn, target):
    """The failure this whole project exists to prevent, via the new path."""
    _sync(conn, target)
    again = _sync(conn, target)
    assert again.created == 0
    assert again.updated == 0
    assert again.refreshed == 0
    assert again.cancelled == 0


def test_changing_the_offset_moves_the_warm_ups(conn, target):
    first = _sync(conn, target)
    _set_warmup(conn, 30)
    second = _sync(conn, target)

    assert second.created == 0
    # Every warm-up moved, and every game re-rendered because its body now
    # states a different arrival time.
    assert second.updated == first.fixtures_seen
    assert second.refreshed == first.fixtures_seen


def test_switching_it_off_cancels_the_warm_ups_without_holding(conn, target, tmp_path):
    first = _sync(conn, target)
    _set_warmup(conn, 0)
    second = _sync(conn, target)

    assert second.status == "ok", second.held
    assert second.cancelled == first.fixtures_seen
    remaining = {p.stem for p in (tmp_path / "out").rglob("*.ics")}
    assert not any(warmup.is_synthetic(uid) for uid in remaining)


def test_the_api_reports_which_game_a_warm_up_belongs_to(conn, target):
    _sync(conn, target)
    stored = repo.stored_events(
        conn, start="2026-01-01T00:00:00+00:00", end="2027-01-01T00:00:00+00:00"
    )
    warm = [s for s in stored if s.event.is_warmup]
    assert warm, "warm-ups should survive the round trip through event_content"
    for item in warm:
        assert item.event.warmup_for == item.event.uid.removeprefix(warmup.PREFIX)
