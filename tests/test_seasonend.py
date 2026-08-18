"""What happens when a season finishes.

Two steps, a month apart: tell somebody, then stop polling. The tests that
matter most are the ones asserting what it does *not* do — the calendar is never
touched, and nothing is announced twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from calsync import db, dormancy, notify, repo, seasonend
from calsync.secrets import SecretStore

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('mira', 'Mira', 'M', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('a', 'mira', 'Wrens', 'soccer', 'UTC');
        INSERT INTO sources (id, activity_id, kind, shape) VALUES ('s', 'a', 'teamreach', 'feed');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def secrets(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text('{"pushover_token": "t", "pushover_user": "u"}')
    path.chmod(0o600)
    return SecretStore(path=path, environ={})


class Pushed:
    def __init__(self):
        self.sent = []

    def __call__(self, _config, _secrets, message, *, title="", url=None, url_title=None):
        self.sent.append({"title": title, "message": message, "url": url})


def season_ended(conn, days_ago):
    conn.execute(
        "INSERT INTO event_state (uid, source_id, collection, content_hash, starts_at)"
        " VALUES (?, 's', 'games', 'h', ?)",
        (f"e{days_ago}", (NOW - timedelta(days=days_ago)).isoformat()),
    )
    conn.commit()


def review(conn, secrets, sender):
    return seasonend.review(conn, repo.get_source(conn, "s"), now=NOW,
                            secrets=secrets, base_url="http://box:8730", sender=sender)


# --- a running season -------------------------------------------------------


def test_a_running_season_is_left_entirely_alone(conn, secrets):
    season_ended(conn, 3)
    pushed = Pushed()

    outcome = review(conn, secrets, pushed)

    assert outcome.stage == dormancy.RUNNING
    assert not outcome.acted
    assert pushed.sent == []
    assert repo.get_source(conn, "s").enabled


# --- one month: tell somebody -----------------------------------------------


def test_a_month_past_the_last_event_sends_one_notification(conn, secrets):
    season_ended(conn, 35)
    pushed = Pushed()

    outcome = review(conn, secrets, pushed)

    assert outcome.notified and not outcome.disabled
    assert "season looks finished" in pushed.sent[0]["title"]
    assert "35 days ago" in pushed.sent[0]["message"]
    assert pushed.sent[0]["url"] == "http://box:8730/sources/s"


def test_polling_continues_after_the_nudge(conn, secrets):
    """A month is a suggestion, not a decision."""
    season_ended(conn, 35)
    review(conn, secrets, Pushed())

    assert repo.get_source(conn, "s").enabled


def test_the_nudge_is_sent_once_not_every_poll(conn, secrets):
    """Twenty minutes apart, forever, is how a notification stops being read."""
    season_ended(conn, 35)
    pushed = Pushed()

    for _ in range(5):
        review(conn, secrets, pushed)

    assert len(pushed.sent) == 1


# --- two months: stop polling -----------------------------------------------


def test_two_months_past_stops_the_polling(conn, secrets):
    season_ended(conn, 70)
    pushed = Pushed()

    outcome = review(conn, secrets, pushed)

    assert outcome.disabled
    assert not repo.get_source(conn, "s").enabled
    assert "polling stopped" in pushed.sent[0]["title"]


def test_shutting_off_never_touches_the_calendar(conn, secrets):
    """By two months past, every event of the season is in the past.

    Cancelling them would delete last spring's games — the record of a season
    the kids actually played. No timer gets to make that call.
    """
    season_ended(conn, 70)
    before = repo.event_states(conn, "s")

    review(conn, secrets, Pushed())

    after = repo.event_states(conn, "s")
    assert after == before
    assert not any(state.cancelled for state in after.values())


def test_the_shutoff_is_announced_after_it_has_happened(conn, secrets):
    """So the message describes a fact rather than an intention."""
    season_ended(conn, 70)
    pushed = Pushed()

    review(conn, secrets, pushed)

    assert "stopped checking" in pushed.sent[0]["message"]
    assert "still on the calendar" in pushed.sent[0]["message"]


def test_both_stages_fire_as_a_season_ages(conn, secrets):
    """A nudge at a month, then a shutoff at two — not one or the other."""
    season_ended(conn, 35)
    pushed = Pushed()
    seasonend.review(conn, repo.get_source(conn, "s"), now=NOW, secrets=secrets,
                     sender=pushed)

    later = NOW + timedelta(days=40)
    seasonend.review(conn, repo.get_source(conn, "s"), now=later, secrets=secrets,
                     sender=pushed)

    assert [s["title"] for s in pushed.sent] == [
        "Wrens: season looks finished",
        "Wrens: polling stopped",
    ]
    assert not repo.get_source(conn, "s").enabled


# --- degrading -------------------------------------------------------------


def test_without_pushover_configured_it_still_shuts_off_quietly(conn, tmp_path):
    """Not having set Pushover up is not a misconfiguration."""
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    empty.chmod(0o600)
    pushed = Pushed()
    season_ended(conn, 70)

    outcome = review(conn, SecretStore(path=empty, environ={}), pushed)

    assert outcome.disabled and not outcome.notified
    assert pushed.sent == []


def test_a_failed_push_does_not_undo_the_shutoff(conn, secrets):
    """The condition is still true tomorrow; the console shows it regardless."""
    def refuse(*_a, **_k):
        raise notify.NotifyError("Pushover refused it (HTTP 429)")

    season_ended(conn, 70)
    outcome = review(conn, secrets, refuse)

    assert outcome.disabled
    assert outcome.errors and "429" in outcome.errors[0]
    assert not repo.get_source(conn, "s").enabled


def test_a_source_kept_across_seasons_is_never_switched_off(conn, secrets):
    """docs/ONBOARDING.md names one: a club team configured once and kept.

    It goes quiet every summer and comes back. Disabling it in July means
    finding out in September.
    """
    conn.execute(
        """UPDATE sources SET config = '{"persists_across_seasons": true}' WHERE id = 's'"""
    )
    conn.commit()
    season_ended(conn, 100)
    pushed = Pushed()

    outcome = review(conn, secrets, pushed)

    assert not outcome.disabled
    assert repo.get_source(conn, "s").enabled
    assert "quiet for 100 days" in pushed.sent[0]["title"]
    assert "polling continues" in pushed.sent[0]["message"]
