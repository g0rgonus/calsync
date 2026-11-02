"""No household is baked into the code.

These tests configure a *different* family — different names, a different
sport, a different calendar split, a different title convention — and assert
the same code produces their conventions rather than the author's.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from calsync.db import BUILTIN_SPORTS, open_db
from calsync.models import Activity, Child, Event, Venue
from calsync.normalize import title as title_norm
from calsync.routing import collection_for
from calsync.settings import Settings, set_setting

FIXTURE = Path(__file__).parent / "fixtures" / "player360_sample.ics"


@pytest.fixture()
def conn(tmp_path):
    return open_db(tmp_path / "calsync.db")


@pytest.fixture()
def settings(conn):
    return Settings.load(conn)


def _event(**kw) -> Event:
    base = dict(
        uid="u1",
        activity_id="a1",
        starts_at=datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc),
        is_game=True,
        tz="America/Chicago",
        opponent="Wampas",
        home=True,
    )
    return Event(**{**base, **kw})


def _activity(**kw) -> Activity:
    base = dict(
        id="a1", child_id="c1", name="Hoth Tauntauns", sport="hockey",
        emoji="🏒", tz="America/Chicago",
    )
    return Activity(**{**base, **kw})


# --- schema / seed ----------------------------------------------------------


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    open_db(path).close()
    conn = open_db(path)            # second run must not fail or duplicate
    count = conn.execute("SELECT COUNT(*) c FROM sports").fetchone()["c"]
    assert count == len(BUILTIN_SPORTS)


def test_sports_are_prepopulated_and_extensible(conn):
    soccer = conn.execute("SELECT * FROM sports WHERE id='soccer'").fetchone()
    assert soccer["emoji"] == "⚽️"
    assert soccer["builtin"] == 1

    conn.execute(
        "INSERT INTO sports (id, name, emoji, builtin) VALUES ('fencing','Fencing','🤺',0)"
    )
    conn.commit()
    assert conn.execute("SELECT emoji FROM sports WHERE id='fencing'").fetchone()[0] == "🤺"


def test_operator_edits_survive_remigration(tmp_path):
    path = tmp_path / "y.db"
    conn = open_db(path)
    set_setting(conn, "collection_template", "{child}")
    conn.execute("UPDATE sports SET emoji='⚽' WHERE id='soccer'")
    conn.commit()
    conn.close()

    conn = open_db(path)   # migrate again
    assert Settings.load(conn).collection_template == "{child}"
    assert conn.execute("SELECT emoji FROM sports WHERE id='soccer'").fetchone()[0] == "⚽"


def test_radicale_url_is_configurable(conn):
    assert Settings.load(conn).radicale_url == "http://localhost:5232"
    set_setting(conn, "radicale_url", "https://dav.example.org/calsync")
    assert Settings.load(conn).radicale_url == "https://dav.example.org/calsync"


# --- routing ----------------------------------------------------------------

BEN = Child(id="c1", name="Ben", initial="B", birth_order=1)
JAINA = Child(id="c2", name="Jaina", initial="J", birth_order=2)
ALLANA = Child(id="c3", name="Allana", initial="A", birth_order=3)


def test_default_routing_splits_by_type(conn, settings):
    assert collection_for(_event(is_game=True), _activity(), BEN, settings) == "games"
    assert collection_for(_event(is_game=False), _activity(), BEN, settings) == "practices"


def test_routing_by_child(conn):
    set_setting(conn, "collection_template", "{child}")
    s = Settings.load(conn)
    assert collection_for(_event(is_game=True), _activity(), BEN, s) == "ben"
    assert collection_for(_event(is_game=False), _activity(), BEN, s) == "ben"


def test_routing_by_child_and_type(conn):
    set_setting(conn, "collection_template", "{child}-{type}")
    s = Settings.load(conn)
    assert collection_for(_event(is_game=True), _activity(), BEN, s) == "ben-games"
    assert collection_for(_event(is_game=False), _activity(), JAINA, s) == "jaina-practices"


def test_routing_to_a_single_calendar(conn):
    set_setting(conn, "collection_template", "family")
    s = Settings.load(conn)
    assert collection_for(_event(is_game=True), _activity(), BEN, s) == "family"
    assert collection_for(_event(is_game=False), _activity(), ALLANA, s) == "family"


def test_collection_labels_are_renameable(conn):
    set_setting(conn, "collection_game_label", "Matches")
    set_setting(conn, "collection_practice_label", "Training")
    s = Settings.load(conn)
    assert collection_for(_event(is_game=True), _activity(), BEN, s) == "matches"
    assert collection_for(_event(is_game=False), _activity(), BEN, s) == "training"


def test_collection_names_are_slugified(conn):
    set_setting(conn, "collection_template", "{activity} {type}")
    s = Settings.load(conn)
    got = collection_for(_event(is_game=True), _activity(name="Hoth Tauntauns U12"), BEN, s)
    assert got == "hoth-tauntauns-u12-games"


# --- titles -----------------------------------------------------------------


def test_default_title_convention(conn, settings):
    assert title_norm.render(_event(), _activity(), [BEN], settings) == "Ben 🏒 vs Wampas"


def test_title_template_is_configurable(conn):
    set_setting(conn, "title_template", "{emoji} {kids}: {detail}")
    s = Settings.load(conn)
    assert title_norm.render(_event(), _activity(), [BEN], s) == "🏒 Ben: vs Wampas"


def test_title_can_include_venue_for_deployments_that_want_it(conn):
    set_setting(conn, "title_template", "{kids} {emoji} {detail} @ {venue}")
    s = Settings.load(conn)
    event = _event(venue=Venue(raw="Echo Base Ice Rink", name="Echo Base Ice Rink"))
    assert title_norm.render(event, _activity(), [BEN], s).endswith("@ Echo Base Ice Rink")


def test_multi_kid_style_names(conn):
    set_setting(conn, "multi_kid_style", "names")
    s = Settings.load(conn)
    assert title_norm.render(_event(), _activity(), [BEN, JAINA], s) == "Ben+Jaina 🏒 vs Wampas"


def test_all_kids_label_and_threshold_are_configurable(conn):
    set_setting(conn, "all_kids_label", "The Rebels")
    set_setting(conn, "all_kids_threshold", "2")
    s = Settings.load(conn)
    assert title_norm.render(_event(), _activity(), [BEN, JAINA], s) == "The Rebels 🏒 vs Wampas"


def test_home_away_markers_are_configurable(conn):
    set_setting(conn, "home_marker", "v")
    set_setting(conn, "away_marker", "away to")
    s = Settings.load(conn)
    assert title_norm.render(_event(home=True), _activity(), [BEN], s) == "Ben 🏒 v Wampas"
    assert (
        title_norm.render(_event(home=False), _activity(), [BEN], s)
        == "Ben 🏒 away to Wampas"
    )


def test_empty_detail_leaves_no_dangling_separator(conn, settings):
    event = _event(opponent=None, detail=None)
    assert title_norm.render(event, _activity(), [BEN], settings) == "Ben 🏒"


def test_activity_emoji_overrides_the_sport_default(conn, settings):
    got = title_norm.render(_event(), _activity(emoji="🥅"), [BEN], settings)
    assert got.startswith("Ben 🥅")


# --- safety thresholds ------------------------------------------------------


def test_disappearance_thresholds_are_configurable(conn):
    from calsync.diff import diff_poll

    set_setting(conn, "max_disappearance_count", "10")
    set_setting(conn, "max_disappearance_pct", "0.90")
    s = Settings.load(conn)

    known = {f"u{i}": "h" for i in range(10)}
    d = diff_poll(
        [], known, now=datetime.now(timezone.utc),
        max_pct=s.max_disappearance_pct, max_count=s.max_disappearance_count,
    )
    # 100% missing still exceeds a 90% ceiling — the guard cannot be turned off
    # by loosening it to something merely permissive.
    assert d.is_anomalous
