"""Being told that something is waiting.

The interesting assertions are the ones about *not* sending. A queue that pushes
on every poll is muted within a day, and a muted notification is worse than none
— it trains you to ignore the one signal that means events are off the calendar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from calsync import db, enrichment, matrix, notify, repo
from calsync.sync import sync_source
from calsync.targets import build

FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_otters_sample.ics"
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
        -- Deliberately NOT "Otters". The activity's own name feeds
        -- `Activity.known_tokens`, so calling it Otters would let the parser
        -- recognise every fixture in the Otters feed and this file would test a
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
    assert "Otters" in body
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
    next_week = FIXTURE.read_bytes().replace(b"Badgers", b"Kestrels")
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

    repo.add_activity_alias(conn, "a", "Otters")          # answer it
    cleared = _review(conn, target, push)
    assert cleared.held == 0
    assert len(push.sent) == 1

    repo.remove_activity_alias(conn, "a", "Otters")       # and it comes back
    again = _review(conn, target, push)

    assert again.notified
    assert len(push.sent) == 2


def test_an_unresolved_venue_alone_never_pages_anybody(conn, target):
    """It is a real gap and it holds nothing back.

    Paging about it would train somebody to ignore the signal that does mean
    events are off the calendar.
    """
    repo.add_activity_alias(conn, "a", "Otters")
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

    at_a_new_ground = FIXTURE.read_bytes().replace(b"Copperfield", b"Kingsmill")
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


# --- telling the agent ------------------------------------------------------
#
# Outbound only. Hermes reads the room and answers on the API; nothing here
# reads a message back, so there is no /sync loop and no identity model to get
# wrong (docs/MATRIX.md §7).


class Room:
    """A Matrix room that records what was posted."""

    def __init__(self):
        self.posted = []

    def __call__(self, _config, _secrets, body, *, transaction_id, **_kw):
        self.posted.append({"body": body, "txn": transaction_id})
        return "$event"


def _configure_matrix(conn):
    from calsync.settings import set_setting

    set_setting(conn, "matrix_homeserver", "https://matrix.example.org")
    set_setting(conn, "matrix_user_id", "@calsync:example.org")
    set_setting(conn, "matrix_room_id", "!room:example.org")


def _dispatch(conn, target, room, *, body=None):
    source, report = _poll(conn, target, body)
    return enrichment.dispatch(conn, source, report, secrets=Store(), sender=room)


def _payload(post):
    import json as _json

    block = post["body"].split("```json")[1].split("```")[0]
    return _json.loads(block)


def test_open_questions_are_posted_to_the_room(conn, target):
    _configure_matrix(conn)
    room = Room()
    outcome = _dispatch(conn, target, room)

    assert outcome.notified
    assert len(room.posted) == 1, "one message per batch, not one per question"
    payload = _payload(room.posted[0])
    assert payload["source"] == "s"
    assert payload["tasks"], "posted a message with no questions in it"


def test_a_venue_is_asked_about_even_though_it_holds_nothing(conn, target):
    """Dispatch is wider than the hold, and deliberately so.

    A venue nobody has entered does not keep a fixture off the calendar, so it
    never pages a human — but resolving it is the best use of a model this
    project has, so it still gets asked.
    """
    repo.add_activity_alias(conn, "a", "Otters")     # nothing is held any more
    _configure_matrix(conn)
    room = Room()
    outcome = _dispatch(conn, target, room)

    assert outcome.held == 0
    assert outcome.notified, "a queue with nothing held asked nothing"
    kinds = {t["type"] for t in _payload(room.posted[0])["tasks"]}
    assert kinds == {"normalize_venue"}


def test_task_ids_are_stable_across_polls(conn, target):
    """The same unanswered question is the same task every time.

    That is what lets a retry dedupe, and what will let an answer be accepted
    against a task nobody had to remember issuing.
    """
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)
    first = {t["task_id"] for t in _payload(room.posted[0])["tasks"]}

    conn.execute("UPDATE sources SET review_dispatched = NULL WHERE id = 's'")
    conn.commit()
    _dispatch(conn, target, room)

    assert {t["task_id"] for t in _payload(room.posted[1])["tasks"]} == first


def test_the_same_questions_are_not_posted_twice(conn, target):
    _configure_matrix(conn)
    room = Room()
    for _ in range(4):
        _dispatch(conn, target, room)

    assert len(room.posted) == 1


def test_a_retry_of_the_same_batch_reuses_its_transaction_id(conn, target):
    """Matrix dedupes on it, so a timeout cannot double-post the questions."""
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)
    conn.execute("UPDATE sources SET review_dispatched = NULL WHERE id = 's'")
    conn.commit()
    _dispatch(conn, target, room)

    assert room.posted[0]["txn"] == room.posted[1]["txn"]


def test_an_unconfigured_room_records_nothing_so_a_later_setup_still_posts(
    conn, target
):
    """The gap a shared flag with the push would have created."""
    room = Room()
    assert not _dispatch(conn, target, room).notified
    assert room.posted == []
    row = conn.execute("SELECT review_dispatched FROM sources WHERE id='s'").fetchone()
    assert row["review_dispatched"] is None

    _configure_matrix(conn)
    assert _dispatch(conn, target, room).notified


def test_the_push_and_the_room_are_tracked_separately(conn, target):
    """Answering one audience must not silence the other."""
    _configure_matrix(conn)
    push, room = Pushover(), Room()
    _review(conn, target, push)
    _dispatch(conn, target, room)

    assert len(push.sent) == 1 and len(room.posted) == 1

    conn.execute("UPDATE sources SET review_notified = NULL WHERE id = 's'")
    conn.commit()
    _review(conn, target, push)
    _dispatch(conn, target, room)

    assert len(push.sent) == 2, "the push did not re-blaze"
    assert len(room.posted) == 1, "the room re-posted because it shared a flag"


def test_the_payload_says_where_to_answer_and_that_it_will_not_be_applied(
    conn, target
):
    """An agent must not read "accepted" as "done".

    The endpoint stores an answer and cannot apply one, so the message says so
    rather than leaving it to be inferred from the absence of a promise.
    """
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)

    respond = _payload(room.posted[0])["respond_via"]
    assert "/v1/tasks/" in respond["endpoint"]
    assert respond["applied_on_receipt"] is False


def test_dispatching_records_the_questions_it_asked(conn, target):
    """What lets the endpoint refuse an answer to a question nobody asked."""
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)

    asked = repo.list_tasks(conn)
    assert asked, "posted questions but recorded none of them"
    assert {t.id for t in asked} == {
        t["task_id"] for t in _payload(room.posted[0])["tasks"]
    }
    assert all(t.state == repo.OPEN for t in asked)


def test_answering_by_hand_closes_the_task(conn, target):
    """A question nobody will ask again must not sit in the queue.

    The console's own answer form makes the diagnostic disappear, and the next
    dispatch is what notices.
    """
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)
    identity = [t for t in repo.list_tasks(conn) if t.type == "resolve_activity"]
    assert identity

    repo.add_activity_alias(conn, "a", "Otters")
    _dispatch(conn, target, room)

    assert repo.get_task(conn, identity[0].id).state == repo.RESOLVED


def test_answering_the_last_question_closes_its_task(conn, target):
    """The case the test above does not reach.

    That one leaves venue questions outstanding, so the set *changes* and the
    cleanup runs on its way to posting. When the last question is answered the
    set becomes empty, and the cleanup used to sit behind the early return for
    exactly that — so the task stayed `open` for ever. Found on a real stack,
    not here.
    """
    _configure_matrix(conn)
    room = Room()
    # A feed whose only outstanding question is the fixture identity: no
    # unresolved venues left to keep the set non-empty.
    repo.upsert_venue(conn, name="Kingsmere")
    for alias in ("Kingsmere", "Kingsmere Meadow Park",
                  "Kingsmere Meadow Park Soccer Fields", "Copperfield Athletic Complex"):
        repo.add_venue_alias(conn, 1, alias)
    _dispatch(conn, target, room)
    before = repo.list_tasks(conn)
    assert before and all(t.state == repo.OPEN for t in before)

    repo.add_activity_alias(conn, "a", "Otters")
    _dispatch(conn, target, room)

    assert all(t.state == repo.RESOLVED for t in repo.list_tasks(conn)), (
        "a question nobody will ask again is still sitting in the queue"
    )


def test_redispatching_never_discards_an_answer_already_given(conn, target):
    """Task ids are derived, so the same question recurs every poll.

    Re-recording it must not wipe an answer that arrived in between, or a reply
    would be lost to a poll that happened to land a second later.
    """
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)
    task = next(t for t in repo.list_tasks(conn) if t.type == "resolve_activity")

    repo.record_answer(conn, task_id=task.id, answer={"alias": "Otters"},
                       rationale=None, answered_by="hermes",
                       answered_at="2026-03-01T12:00:00+00:00")
    conn.execute("UPDATE sources SET review_dispatched = NULL WHERE id = 's'")
    conn.commit()
    _dispatch(conn, target, room)

    kept = repo.get_task(conn, task.id)
    assert kept.state == repo.ANSWERED
    assert kept.answer == {"alias": "Otters"}


def test_a_refused_post_is_not_recorded_so_it_is_retried(conn, target):
    """Unlike the push: a task that never reaches the agent means no work."""
    _configure_matrix(conn)

    def refusing(*_a, **_k):
        raise matrix.MatrixError("homeserver said no")

    source, report = _poll(conn, target)
    outcome = enrichment.dispatch(conn, source, report, secrets=Store(), sender=refusing)

    assert outcome.errors and not outcome.notified
    row = conn.execute("SELECT review_dispatched FROM sources WHERE id='s'").fetchone()
    assert row["review_dispatched"] is None, "a failed post was recorded as sent"


def test_ten_fixtures_are_one_question_not_ten(conn, target):
    """One answer resolves all of them, so it is one question.

    Ten `resolve_activity` tasks whose answer is a single activity alias would
    cost ten round trips and give ten chances to answer inconsistently. The
    console already collapses this for a human; the room gets the same shape.
    """
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)

    tasks = _payload(room.posted[0])["tasks"]
    identity = [t for t in tasks if t["type"] == "resolve_activity"]

    assert len(identity) == 1, f"asked the same question {len(identity)} times"
    assert len(identity[0]["context"]) > 1, "collapsed away the evidence too"


def test_the_collapsed_question_offers_the_same_answer_the_console_does(conn, target):
    """Ranked by frequency, best first — `inspection.name_candidates`.

    A human clicking the suggested button and an agent taking the first
    candidate have to be choosing from one list, or the two paths can disagree
    about the same feed.
    """
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)

    identity = next(
        t for t in _payload(room.posted[0])["tasks"]
        if t["type"] == "resolve_activity"
    )
    assert identity["candidates"][0] == "Otters"


def test_distinct_venues_stay_distinct_questions(conn, target):
    """Collapsing is only right where one answer covers the lot.

    One venue's address says nothing about another's, so these must not be
    folded together the way the fixture names are.
    """
    _configure_matrix(conn)
    room = Room()
    _dispatch(conn, target, room)

    venues = [
        t for t in _payload(room.posted[0])["tasks"]
        if t["type"] == "normalize_venue"
    ]
    assert len(venues) > 1
    assert all(len(t["context"]) == 1 for t in venues)
