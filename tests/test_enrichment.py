"""Being told that something is waiting.

The interesting assertions are the ones about *not* sending. A queue that pushes
on every poll is muted within a day, and a muted notification is worse than none
— it trains you to ignore the one signal that means events are off the calendar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from calsync import db, enrichment, notify, repo
from calsync.sync import sync_source
from calsync.targets import build

FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_hawks_sample.ics"
NOW = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)


class Pushover:
    """Records what would have been sent."""

    def __init__(self):
        self.sent = []

    def __call__(self, _config, _secrets, message, *, title="", url=None, url_title=None):
        self.sent.append({"title": title, "message": message, "url": url})


class Refusing(Pushover):
    def __call__(self, *args, **kwargs):
        raise notify.NotifyError("pushover said no")


class Store:
    def get(self, ref):
        return {"pushover_token": "t", "pushover_user": "u"}[ref]

    def has(self, ref):
        return True


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('k', 'Kid', 'K', 1);
        -- Deliberately NOT "Hawks". The activity's own name feeds
        -- `Activity.known_tokens`, so calling it Hawks would let the parser
        -- recognise every fixture in the Hawks feed and this file would test a
        -- queue that is always empty.
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('a', 'k', 'Spring Squad', 'soccer', 'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('s', 'a', 'teamreach', 'feed');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def target(tmp_path):
    return build("ics_file", directory=tmp_path / "out")


def _poll(conn, target, body=None):
    source = repo.get_source(conn, "s")
    raw = body if body is not None else FIXTURE.read_bytes()
    return source, sync_source(conn, source, target, now=NOW, raw=raw)


def _review(conn, target, sender, *, base_url="http://box:8730", body=None):
    source, report = _poll(conn, target, body)
    return enrichment.review(conn, source, report, secrets=Store(),
                             base_url=base_url, sender=sender)


# --- it fires when something is genuinely waiting ---------------------------


def test_a_held_queue_is_announced_once(conn, target):
    push = Pushover()
    first = _review(conn, target, push)

    assert first.held > 0, "nothing was held, so this proves nothing"
    assert first.notified
    assert len(push.sent) == 1
    assert "Spring Squad" in push.sent[0]["title"]
    assert str(first.held) in push.sent[0]["title"]


def test_the_notification_links_to_the_page_with_the_answers_on_it(conn, target):
    push = Pushover()
    _review(conn, target, push)

    assert push.sent[0]["url"] == "http://box:8730/review"


def test_it_names_the_questions_but_does_not_paste_the_season_in(conn, target):
    """A notification is a lock screen, not a report."""
    push = Pushover()
    _review(conn, target, push)

    body = push.sent[0]["message"]
    assert "Hawks" in body
    assert body.count("·") <= 6, "unbounded list in a push notification"
    assert "more" in body, "truncated without saying it had truncated"


# --- and stays quiet the rest of the time -----------------------------------


def test_polling_again_with_the_same_questions_sends_nothing(conn, target):
    """The one that decides whether this is usable.

    The poller runs every twenty minutes and the events wait until somebody
    acts, so a per-poll notification is muted by lunchtime.
    """
    push = Pushover()
    _review(conn, target, push)
    for _ in range(5):
        again = _review(conn, target, push)

    assert not again.notified
    assert len(push.sent) == 1, "notified repeatedly for the same unanswered question"


def test_a_genuinely_new_question_notifies_again(conn, target):
    """More events against a known question is not news. A new question is.

    The realistic case: next week's poll carries a fixture against a club nobody
    has seen before, so the set of things needing an answer has genuinely grown.
    """
    push = Pushover()
    _review(conn, target, push)
    assert len(push.sent) == 1

    # Same feed, one opponent renamed — a question that did not exist before.
    next_week = FIXTURE.read_bytes().replace(b"Bruins", b"Kestrels")
    _review(conn, target, push, body=next_week)

    assert len(push.sent) == 2
    assert "Kestrels" in push.sent[1]["message"]


def test_the_same_questions_arriving_with_more_events_stays_quiet(conn, target):
    """The signature is over the questions, not the count."""
    push = Pushover()
    _review(conn, target, push)

    # A poll where nothing about the open questions changed.
    _review(conn, target, push)

    assert len(push.sent) == 1


def test_a_cleared_queue_resets_so_the_next_one_is_announced(conn, target):
    """A flag that latched forever would make this a once-ever notification."""
    push = Pushover()
    _review(conn, target, push)
    assert len(push.sent) == 1

    repo.add_activity_alias(conn, "a", "Hawks")          # answer it
    cleared = _review(conn, target, push)
    assert cleared.held == 0
    assert len(push.sent) == 1

    repo.remove_activity_alias(conn, "a", "Hawks")       # and it comes back
    again = _review(conn, target, push)

    assert again.notified
    assert len(push.sent) == 2


def test_an_unresolved_venue_alone_never_pages_anybody(conn, target):
    """It is a real gap and it holds nothing back.

    Paging about it would train somebody to ignore the signal that does mean
    events are off the calendar.
    """
    repo.add_activity_alias(conn, "a", "Hawks")
    push = Pushover()
    outcome = _review(conn, target, push)

    source, report = _poll(conn, target)
    assert report.diagnostics.get("unresolved_venues"), "no venue gap in this fixture"
    assert outcome.held == 0
    assert push.sent == []


def test_a_new_venue_does_not_repage_a_queue_that_is_already_known(
    conn, target
):
    """The precise thing `BLOCKING_DIAGNOSTICS` is for.

    The venue-only test above exits before the question set is even computed,
    because nothing is held — so it cannot catch venues being counted as
    blocking. This one can: a queue is already open and announced, then next
    week's poll brings a game at a ground nobody has entered yet. The questions
    holding events back have not changed, so there is nothing new to say.
    """
    push = Pushover()
    _review(conn, target, push)
    assert len(push.sent) == 1

    at_a_new_ground = FIXTURE.read_bytes().replace(b"Stoney Run", b"Kingsmill")
    outcome = _review(conn, target, push, body=at_a_new_ground)

    source, report = _poll(conn, target, at_a_new_ground)
    assert any("Kingsmill" in v for v in report.diagnostics["unresolved_venues"]), (
        "the new venue did not register, so this proves nothing"
    )
    assert outcome.held > 0, "nothing held, so the early return makes this vacuous"
    assert len(push.sent) == 1, "paged about a venue, which holds nothing back"


def test_the_hold_being_off_means_nothing_to_announce(conn, target):
    from calsync.settings import set_setting

    set_setting(conn, "enrichment_collection", "")
    push = Pushover()
    outcome = _review(conn, target, push)

    assert outcome.held == 0
    assert push.sent == []


# --- failure is not the poller's problem ------------------------------------


def test_a_refused_push_is_recorded_and_not_retried_forever(conn, target):
    """The condition is still true next poll, and the console shows it anyway."""
    outcome = _review(conn, target, Refusing())

    assert outcome.errors
    assert not outcome.notified
    # Still recorded, or a deployment whose Pushover is misconfigured re-tries
    # a failing HTTP call every twenty minutes for the rest of the season.
    row = conn.execute("SELECT review_notified FROM sources WHERE id = 's'").fetchone()
    assert row["review_notified"]
