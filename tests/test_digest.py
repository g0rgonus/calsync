"""What's on tomorrow, as a message.

The digest reads the receipt — `event_content`, written after each event reached
the calendar — rather than re-parsing the feeds or unpicking the calendar's own
rendered titles. So the tests that matter are about agreement and about what it
must *not* do: report something nobody's phone has, quietly under-report, or
change anything.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from calsync import db, digest, matrix, repo
from calsync.secrets import SecretStore
from calsync.sync import sync_source
from calsync.targets import build

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


@pytest.fixture
def synced(conn, tmp_path):
    """A real sync, so there is a real calendar to agree with.

    The digest no longer touches the network at all, so a fixture that stubbed a
    fetcher would be testing nothing. What it reads is what a sync wrote.
    """
    source = repo.list_sources(conn)[0]
    sync_source(conn, source, build("ics_file", directory=tmp_path / "out"),
                now=NOW, raw=COMETS)
    return conn


def _calendar_titles(tmp_path):
    return {
        line.split(":", 1)[1].strip()
        for path in (tmp_path / "out").rglob("*.ics")
        for line in path.read_text().splitlines()
        if line.startswith("SUMMARY:")
    }


def test_it_lists_what_starts_in_the_window(synced):
    result = digest.collect(synced, now=NOW)

    assert not result.empty
    assert all(NOW <= e.starts_at <= NOW + timedelta(hours=24) for e in result.entries)


def test_the_message_says_what_the_calendar_says(synced, tmp_path):
    """The reason for reading the receipt rather than re-parsing the feed.

    Re-deriving reports what the *feed* holds, which is not always what was
    written — a held or failed poll leaves the two disagreeing, and a message
    announcing a game nobody's phone has is wrong in the direction that gets
    somebody driving to a field.
    """
    result = digest.collect(synced, now=NOW)

    assert {e.title for e in result.entries} <= _calendar_titles(tmp_path)
    assert any("Millie" in e.title for e in result.entries)


def test_times_are_shown_in_the_venue_timezone(synced):
    """Same rule the event bodies follow.

    A parent reading a time rendered in their own timezone reads a time that is
    not the start time.
    """
    entry = digest.collect(synced, now=NOW).entries[0]

    assert entry.local.tzinfo is not None
    assert entry.local.strftime("%H:%M") in entry.line()
    assert entry.local.hour != entry.starts_at.hour, "not converted out of UTC"


def test_a_source_that_has_not_been_polled_is_named_rather_than_read(conn):
    """A digest that silently omits a team reads as "nothing on today".

    That is the one wrong answer a schedule message can give — and the digest
    can no longer go and look, so saying so is the whole of its defence. The
    url_template here would fail loudly if anything tried to fetch it.
    """
    result = digest.collect(conn, now=NOW)

    assert result.empty
    assert result.stale == ["Comets"]
    assert "Not polled recently: Comets" in result.text()
    assert "may be out of date" in result.text()


def test_a_failing_feed_is_named_even_though_older_events_are_still_readable(synced):
    """The stored copy outlives the feed, so the message has to carry the doubt."""
    repo.record_source_error(synced, "s", "connection refused")
    synced.commit()

    result = digest.collect(synced, now=NOW)

    assert not result.empty, "a broken feed should not lose events already written"
    assert result.stale == ["Comets"]


def test_a_paused_source_is_still_on_the_calendar_and_so_is_in_the_digest(synced):
    """Deliberately changed when the digest stopped re-parsing feeds.

    Pausing stops polling; it does not take a single event off anybody's
    calendar. The family still has a game on Saturday, so omitting it was the
    same silent under-report the `stale` list exists to prevent. Retiring is the
    operation that genuinely removes events, and `retire.py` cancels every one
    before it disables anything — so those stay out, by being cancelled.
    """
    repo.set_enabled(synced, "s", False)

    result = digest.collect(synced, now=NOW)

    assert not result.empty
    assert result.stale == [], "a deliberate pause is not a fault to report"


def test_a_cancelled_event_is_not_on(synced):
    """A tombstone is how a deletion propagates, not something that is on."""
    before = len(digest.collect(synced, now=NOW).entries)
    uid = next(
        item.event.uid
        for item in repo.stored_events(
            synced, start=NOW.isoformat(),
            end=(NOW + timedelta(hours=24)).isoformat(),
        )
    )
    repo.mark_event_cancelled(synced, uid)
    synced.commit()

    assert len(digest.collect(synced, now=NOW).entries) == before - 1


def test_an_empty_day_says_so_plainly(synced):
    quiet = NOW - timedelta(days=200)
    assert "nothing on" in digest.collect(synced, now=quiet).text()


def test_collecting_writes_nothing(synced, tmp_path):
    """A read that advanced sync state would make "what's on?" a risky command.

    Asserted against the rows rather than only the file: the schema runs in WAL
    mode, so the main database file does not necessarily change on a write and a
    bytes comparison alone can pass without proving anything.
    """
    def snapshot():
        return {
            table: list(map(tuple, synced.execute(f"SELECT * FROM {table}")))
            for table in ("event_state", "event_content", "poll_runs", "sources")
        }

    before, before_bytes = snapshot(), (tmp_path / "calsync.db").read_bytes()
    digest.collect(synced, now=NOW)
    digest.collect(synced, now=NOW - timedelta(days=200))

    assert snapshot() == before
    assert (tmp_path / "calsync.db").read_bytes() == before_bytes


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


# --- pushover ---------------------------------------------------------------


def test_a_push_carries_both_credentials_and_a_link():
    from calsync import notify

    sent = {}

    class Server:
        def __call__(self, request, timeout=None):
            from urllib.parse import parse_qs
            sent.update({k: v[0] for k, v in parse_qs(request.data.decode()).items()})
            return _Reply(200, {"status": 1})

    class Store:
        def get(self, ref):
            return {"pushover_token": "app-token", "pushover_user": "user-key"}[ref]

    notify.send(notify.PushoverConfig(), Store(), "Season looks finished",
                title="Comets", url="http://box:8730/sources/s", opener=Server())

    assert sent["token"] == "app-token" and sent["user"] == "user-key"
    assert sent["title"] == "Comets"
    assert sent["url"] == "http://box:8730/sources/s"


def test_pushover_refusing_is_an_error_not_a_silent_success():
    import urllib.error

    from calsync import notify

    class Store:
        def get(self, _ref):
            return "x"

    def refused(_request, timeout=None):
        raise urllib.error.HTTPError(
            notify.API, 429, "Too Many Requests", {},
            __import__("io").BytesIO(b'{"errors":["rate limited"]}'))

    with pytest.raises(notify.NotifyError, match="rate limited"):
        notify.send(notify.PushoverConfig(), Store(), "x", opener=refused)


def test_a_credential_never_appears_in_a_push_error():
    from calsync import notify

    class Store:
        def get(self, _ref):
            raise __import__("calsync.secrets", fromlist=["SecretError"]).SecretError(
                "no secret for 'pushover_token'")

    with pytest.raises(notify.NotifyError) as raised:
        notify.send(notify.PushoverConfig(), Store(), "x")
    assert "pushover_token" in str(raised.value)


# --- when it goes out -------------------------------------------------------


from datetime import date  # noqa: E402

MORNING = datetime(2026, 3, 4, 7, 30, tzinfo=timezone.utc)


def test_no_send_time_means_never():
    """A deployment that never asked for a digest should not get one."""
    assert not digest.due(now_local=MORNING, send_at="", last_sent_on=None)
    assert not digest.due(now_local=MORNING, send_at="   ", last_sent_on=None)


def test_it_is_due_once_the_hour_has_passed():
    assert digest.due(now_local=MORNING, send_at="07:00", last_sent_on=None)
    assert not digest.due(now_local=MORNING, send_at="08:00", last_sent_on=None)


def test_it_goes_out_once_a_day():
    assert not digest.due(now_local=MORNING, send_at="07:00",
                          last_sent_on=date(2026, 3, 4))
    assert digest.due(now_local=MORNING, send_at="07:00",
                      last_sent_on=date(2026, 3, 3))


def test_late_beats_never():
    """A poller started at 09:00 with a 07:00 digest still sends today's.

    Otherwise restarting the container in the morning silently costs the day's
    message, which is not a rule anyone remembers when wondering where it went.
    """
    nine = MORNING.replace(hour=9)
    assert digest.due(now_local=nine, send_at="07:00", last_sent_on=None)


def test_a_malformed_time_sends_nothing_rather_than_everything():
    for bad in ("half seven", "7", "25:00", ":"):
        assert not digest.due(now_local=MORNING, send_at=bad, last_sent_on=None), bad
