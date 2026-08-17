"""What's on tomorrow, as a message.

The digest re-derives from the feeds rather than reading the calendar back,
because the calendar holds renders and not data (docs/API.md). The tests that
matter are the ones about what it must *not* do: change anything, or quietly
under-report.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from calsync import db, digest, matrix, repo
from calsync.secrets import SecretStore

FIXTURES = Path(__file__).parent / "fixtures"
COMETS = (FIXTURES / "teamreach_comets_sample.ics").read_bytes()

#: The comets fixture's first practice is 2026-03-05 00:00Z.
NOW = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('millie', 'Millie', 'M', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('a', 'millie', 'Comets', 'soccer', 'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape, url_template)
             VALUES ('s', 'a', 'teamreach', 'feed', 'https://feed.example/c.ics');
        """
    )
    connection.commit()
    return connection


def feed(_assembled, **_kw):
    return COMETS


def dead(_assembled, **_kw):
    raise OSError("connection refused")


def test_it_lists_what_starts_in_the_window(conn):
    result = digest.collect(conn, now=NOW, fetcher=feed)

    assert not result.empty
    assert all(NOW <= e.starts_at <= NOW + timedelta(hours=24) for e in result.entries)


def test_titles_come_from_the_real_renderer(conn):
    """So the message agrees with the calendar rather than approximating it."""
    result = digest.collect(conn, now=NOW, fetcher=feed)
    assert any("Millie" in e.title for e in result.entries)


def test_times_are_shown_in_the_venue_timezone(conn):
    """Same rule the event bodies follow.

    A parent reading a time rendered in their own timezone reads a time that is
    not the start time.
    """
    result = digest.collect(conn, now=NOW, fetcher=feed)
    entry = result.entries[0]

    assert entry.local.tzinfo is not None
    assert entry.local.strftime("%H:%M") in entry.line()
    assert entry.local.hour != entry.starts_at.hour, "not converted out of UTC"


def test_a_dead_feed_is_named_rather_than_dropped(conn):
    """A digest that silently omits a team reads as "nothing on today".

    That is the one wrong answer a schedule message can give.
    """
    result = digest.collect(conn, now=NOW, fetcher=dead)

    assert result.unavailable == ["Comets"]
    assert "Could not read: Comets" in result.text()
    assert "may be incomplete" in result.text()


def test_a_disabled_source_is_not_in_the_digest(conn):
    """Paused or retired means it is not on the calendar either."""
    repo.set_enabled(conn, "s", False)
    assert digest.collect(conn, now=NOW, fetcher=feed).empty


def test_an_empty_day_says_so_plainly(conn):
    quiet = NOW - timedelta(days=200)
    assert "nothing on" in digest.collect(conn, now=quiet, fetcher=feed).text()


def test_collecting_writes_nothing(conn, tmp_path):
    """A read that advanced sync state would make "what's on?" a risky command."""
    before = (tmp_path / "calsync.db").read_bytes()
    digest.collect(conn, now=NOW, fetcher=feed)
    digest.collect(conn, now=NOW, fetcher=dead)

    assert repo.event_states(conn, "s") == {}
    assert list(conn.execute("SELECT * FROM poll_runs")) == []
    assert (tmp_path / "calsync.db").read_bytes() == before


# --- sending ----------------------------------------------------------------


class Homeserver:
    def __init__(self, status=200):
        self.status, self.sent = status, []

    def __call__(self, request, timeout=None):
        self.sent.append((request.full_url, json.loads(request.data)))
        return _Reply(self.status, {"event_id": "$abc"})


class _Reply:
    def __init__(self, status, body):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def secrets(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text('{"matrix_access_token": "syt_secret"}')
    path.chmod(0o600)
    return SecretStore(path=path, environ={})


CONFIG = matrix.MatrixConfig(
    homeserver="https://matrix.example.org",
    user_id="@calsync:example.org",
    room_id="!room:example.org",
)


def test_a_message_is_sent_to_the_room(secrets):
    server = Homeserver()
    event_id = matrix.send(CONFIG, secrets, "**Wednesday**\n- 19:00  Millie ⚽️",
                           transaction_id="calsync-digest-2026-03-05", opener=server)

    url, payload = server.sent[0]
    assert event_id == "$abc"
    # The room id is fully percent-encoded, "!" included — legal, and what
    # keeps a room id with a colon in it from splitting the path.
    assert "%21room%3Aexample.org" in url
    assert payload["body"].startswith("**Wednesday**")
    assert "<b>Wednesday</b>" in payload["formatted_body"]


def test_the_transaction_id_is_the_day_so_a_retry_cannot_double_post(secrets):
    """Matrix deduplicates on it. A random id would make retrying the risk."""
    server = Homeserver()
    for _ in range(2):
        matrix.send(CONFIG, secrets, "body",
                    transaction_id="calsync-digest-2026-03-05", opener=server)

    first, second = server.sent[0][0], server.sent[1][0]
    assert first == second, "two posts of the same day used different ids"
    assert first.endswith("calsync-digest-2026-03-05")


def test_html_is_escaped_not_interpreted(secrets):
    """Coaches type venue names. One of them will contain an angle bracket."""
    server = Homeserver()
    matrix.send(CONFIG, secrets, "- 19:00  <script>alert(1)</script>",
                transaction_id="t", opener=server)

    assert "<script>" not in server.sent[0][1]["formatted_body"]
    assert "&lt;script&gt;" in server.sent[0][1]["formatted_body"]


def test_an_unconfigured_room_refuses_before_reaching_the_network(secrets):
    def explode(*_a, **_k):
        raise AssertionError("should not have tried to send")

    with pytest.raises(matrix.MatrixError, match="not configured"):
        matrix.send(matrix.MatrixConfig(), secrets, "body",
                    transaction_id="t", opener=explode)


def test_a_refused_message_says_what_the_homeserver_said(secrets):
    with pytest.raises(matrix.MatrixError, match="403"):
        matrix.send(CONFIG, secrets, "body", transaction_id="t",
                    opener=Homeserver(status=403))
