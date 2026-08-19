"""The R1-R8 server requirements, and one real sync, against a live Radicale.

`docs/deployment/radicale.md` states eight requirements and gives curl snippets
for checking them. CLAUDE.md records that they were verified by hand once. That
is the weakest link in the project: every other layer is covered by fast unit
tests, and the one place calsync meets a real server was checked manually
against a server that no longer exists, on a version nobody pinned.

Two of these are worth more than the rest. R4 and R5 fail *silently* — a server
that normalizes properties away still returns 200, still stores the event, and
simply loses the provenance and the map pin. Nothing downstream notices until
somebody taps a location in a calendar and lands in the wrong car park.

Skipped unless a stack is up, so the ordinary suite stays offline and fast:

    scripts/dev-stack.sh
    CALSYNC_ACCEPTANCE=1 .venv/bin/pytest tests/test_acceptance.py -v
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from calsync import db, repo
from calsync.secrets import SecretStore
from calsync.sync import sync_source
from calsync.render import RenderedEvent
from calsync.targets import build
from calsync.targets.http import HttpTransport

pytestmark = pytest.mark.skipif(
    not os.environ.get("CALSYNC_ACCEPTANCE"),
    reason="needs a live stack: scripts/dev-stack.sh, then CALSYNC_ACCEPTANCE=1",
)

# Through the proxy, at the path a phone subscribes to, because that is the
# deployment — nothing publishes 5232 any more. It also means these checks
# cover the one route that needs the server's cooperation: CalDAV clients
# navigate by the absolute hrefs in a PROPFIND body, so a path-mounted
# Radicale that is not told its prefix hands back hrefs to a different site.
BASE = os.environ.get("CALSYNC_ACCEPTANCE_URL", "http://localhost:8730/cal")
USER = os.environ.get("CALSYNC_ACCEPTANCE_USER", "calsync")
READER = os.environ.get("CALSYNC_ACCEPTANCE_READER", "calreader")
def _configured(name: str) -> str:
    """A credential, from the environment or from `.env`.

    Both, because `.env` is where the stack keeps these and where dev-stack.sh
    writes them — reading it means the documented
    `CALSYNC_ACCEPTANCE=1 pytest` still works straight afterwards, with nothing
    to export by hand. There is no secrets file on the host any more.
    """
    if os.environ.get(name):
        return os.environ[name]
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip()
    return ""


WRITER_PASSWORD = _configured("CALSYNC_SECRET_RADICALE_PASSWORD")
#: Its own value, not the writer's. The two accounts are separate credentials in
#: a real deployment, and R8 is only worth anything if it is checked as one.
READER_PASSWORD = _configured("CALSYNC_SECRET_RADICALE_READER_PASSWORD")

FIXTURE = Path(__file__).parent / "fixtures" / "teamreach_wrens_sample.ics"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

#: An upstream-shaped resource name: mixed case and hyphens (R6). UIDs come from
#: the feed and are not ours to rewrite into something tidier.
UID = "360Player-event-4823901.ics"

def _probe() -> str:
    """R4/R5 probe, serialized by calsync's own writer.

    Deliberately not a hand-written string. A literal here would test my typing;
    what R4 and R5 need to know is whether the bytes *calsync* produces survive
    the server — including how the icalendar library folds long lines and
    escapes the comma inside `geo:lat,lon`, which is precisely where an exact
    pin gets quietly turned into an approximate one.
    """
    from calsync.targets.ics_file import to_ics

    event = RenderedEvent(
        uid="360Player-event-4823901",
        collection="acceptance",
        title="Acceptance probe",
        starts_at=datetime(2026, 3, 11, 23, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 3, 12, 0, 0, tzinfo=timezone.utc),
        tz="America/New_York",
        body="",
        # Exactly what `render()` composes: name, then address, in one line.
        # Building it by hand as the address alone made this probe test my
        # typing rather than what actually reaches a calendar.
        location_text="Kingsmere Meadow Park, 1 Kingsmere Rd, Halden VA",
        venue_name="Kingsmere Meadow Park",
        is_game=True,
        provenance={"SOURCE": "acceptance", "HASH": "0123456789abcdef"},
    )
    return to_ics(event).decode()


PROBE = None  # built lazily by the fixture below, once RenderedEvent is imported


@pytest.fixture(scope="module")
def password():
    if not WRITER_PASSWORD:
        pytest.skip("no CALSYNC_SECRET_RADICALE_PASSWORD — run scripts/dev-stack.sh")
    return WRITER_PASSWORD


@pytest.fixture
def reader_password():
    if not READER_PASSWORD:
        pytest.skip("no CALSYNC_SECRET_RADICALE_READER_PASSWORD — run scripts/dev-stack.sh")
    return READER_PASSWORD


def header(response, name: str) -> str | None:
    """Case-insensitive, for the same reason `caldav._header` is.

    This helper first spelled it `ETag`, urllib had title-cased it to `Etag`,
    and the test reported the server as non-compliant when the server was fine.
    Exactly the bug it was written to catch, repeated one layer up.
    """
    wanted = name.casefold()
    return next((v for k, v in (response.headers or {}).items()
                 if k.casefold() == wanted), None)


def _unfold(text: str) -> str:
    """Undo RFC 5545 line folding, which is not a change to the value."""
    return text.replace("\r\n ", "").replace("\n ", "")


def _property(text: str, name: str) -> str:
    for line in text.splitlines():
        if line.startswith(name):
            return line
    return ""


def call(method, url, *, user=USER, password=None, body=None, headers=None):
    transport = HttpTransport(username=user, password=password)
    return transport(method, url, body=body.encode() if body else None,
                     headers=headers or {})


#: Unique per run. Radicale keeps its storage between runs where SQLite does
#: not, so a suite that assumes a clean server passes once and then fails
#: forever on `If-None-Match: *` against its own leftovers. Isolating the
#: collection is cheaper than cleaning up reliably after a failed run.
RUN = f"acc{os.getpid()}"


@pytest.fixture(scope="module")
def collection(password):
    """A scratch calendar, created exactly how the target creates one (R7).

    MKCALENDAR, not MKCOL: Radicale answers MKCOL under a user principal with
    403, and a probe that used the wrong verb would report a rights problem
    that does not exist.
    """
    url = f"{BASE}/{USER}/{RUN}/"
    response = call("MKCALENDAR", url, password=password)
    assert response.status in (201, 405, 409), (
        f"MKCALENDAR returned {response.status}; the target treats 405/409 as "
        "'already exists' and anything else as a failure"
    )
    yield url
    call("DELETE", url, password=password)


# --- the server requirements ------------------------------------------------


def test_r1_reachable(password):
    assert call("GET", f"{BASE}/", password=password).status in (200, 301, 302, 207)


def test_r2_put_returns_an_etag(collection, password):
    """Without one there is no conflict detection and a concurrent edit is lost."""
    response = call("PUT", collection + UID, password=password, body=_probe(),
                    headers={"Content-Type": "text/calendar; charset=utf-8"})
    assert response.status in (201, 204)
    assert header(response, "ETag"), "no ETag on the PUT response"


def test_r3_a_stale_if_match_is_refused(collection, password):
    """412, not a silent overwrite. The writer must raise rather than clobber."""
    call("PUT", collection + UID, password=password, body=_probe(),
         headers={"Content-Type": "text/calendar; charset=utf-8"})
    response = call("PUT", collection + UID, password=password, body=_probe(),
                    headers={"Content-Type": "text/calendar; charset=utf-8",
                             "If-Match": '"definitely-not-the-current-etag"'})
    assert response.status == 412, f"expected 412, got {response.status}"


def test_r4_unknown_x_properties_survive_a_round_trip(collection, password):
    """Fails silently: the write succeeds and the provenance is simply gone."""
    call("PUT", collection + UID, password=password, body=_probe(),
         headers={"Content-Type": "text/calendar; charset=utf-8"})
    body = _unfold(call("GET", collection + UID, password=password).body.decode())

    assert "X-CALSYNC-SOURCE:acceptance" in body
    assert "X-CALSYNC-HASH:0123456789abcdef" in body


def test_r5_the_location_text_survives_a_round_trip(collection, password):
    """R5 used to be about Apple's exact-pin property, which calsync no longer
    emits — a venue name and street address resolve fine in a maps app, and the
    pin route depended on a coordinate round-trip this very server truncated.

    What still has to survive is the location *text*, because that is now the
    whole of what gets somebody to the right car park.
    """
    call("PUT", collection + UID, password=password, body=_probe(),
         headers={"Content-Type": "text/calendar; charset=utf-8"})
    got = _unfold(call("GET", collection + UID, password=password).body.decode())

    assert "Kingsmere Meadow Park" in got
    assert "1 Kingsmere Rd" in got


def test_r6_an_upstream_resource_name_is_accepted(collection, password):
    """Mixed case and hyphens. UIDs are the feed's, not ours to rewrite."""
    assert call("GET", collection + UID, password=password).status == 200


def test_r8_the_read_only_principal_cannot_write(collection, reader_password):
    """The check that catches a rights file failing open."""
    response = call("PUT", collection + "reader-probe.ics", user=READER,
                    password=reader_password, body=_probe(),
                    headers={"Content-Type": "text/calendar; charset=utf-8"})
    assert response.status in (401, 403), (
        f"the read-only principal wrote successfully ({response.status}) — "
        "the rights file is failing open"
    )


def test_r8_the_read_only_principal_can_read(collection, reader_password):
    assert call("GET", collection + UID,
                user=READER, password=reader_password).status == 200


# --- and one real sync ------------------------------------------------------


@pytest.fixture
def configured(tmp_path, password):
    conn = db.open_db(tmp_path / "calsync.db")
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('mira', 'Mira', 'M', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('mira-soccer-wrens', 'mira', 'Wrens', 'soccer',
                     'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape, staging_collection)
             VALUES ('tr-wrens', 'mira-soccer-wrens', 'teamreach', 'feed',
                     ?);
        """.replace("?", f"'{RUN}-staging'")
    )
    # Route the promoted events somewhere unique too, for the same reason.
    conn.execute("UPDATE settings SET value = ? WHERE key = 'collection_template'",
                 (RUN + "-{type}",))
    conn.commit()
    yield conn
    for suffix in ("-staging", "-games", "-practices"):
        call("DELETE", f"{BASE}/{USER}/{RUN}{suffix}/", password=password)


def _target(password):
    return build(
        "caldav",
        base_url=f"{BASE}/{USER}",
        transport=HttpTransport(username=USER, password=password),
        username=USER,
        password=password,
    )


def test_a_feed_syncs_into_a_real_caldav_server(configured, password):
    """The whole loop against a real server, which nothing else covers.

    Every other test in this suite stops at `RenderedEvent` or at an .ics file
    on disk. This is the only one that proves calsync and Radicale actually
    agree — about collection creation, resource naming, and ETags.
    """
    source = repo.get_source(configured, "tr-wrens")
    report = sync_source(configured, source, _target(password), now=NOW,
                         raw=FIXTURE.read_bytes())

    assert report.status == "ok", report.line()
    assert report.created > 0
    assert not report.errors

    states = repo.event_states(configured, "tr-wrens")
    assert len(states) == report.created
    for state in states.values():
        assert state.remote_etag, "no ETag stored, so a later update cannot be safe"
        assert state.collection == f"{RUN}-staging"

    # And it is genuinely on the server, not merely recorded as written.
    sample = next(iter(states.values()))
    fetched = call("GET", f"{BASE}/{USER}/{RUN}-staging/{sample.remote_id}.ics",
                   password=password)
    assert fetched.status == 200
    assert b"BEGIN:VEVENT" in fetched.body


def test_a_second_sync_writes_nothing(configured, password):
    """The assertion that mattered for ics files matters more against a server."""
    target = _target(password)
    source = repo.get_source(configured, "tr-wrens")
    first = sync_source(configured, source, target, now=NOW, raw=FIXTURE.read_bytes())
    second = sync_source(configured, source, target, now=NOW, raw=FIXTURE.read_bytes())

    assert second.created == 0
    assert second.updated == 0
    assert second.cancelled == 0
    assert second.unchanged == first.created


def test_promotion_relocates_rather_than_duplicating(configured, password):
    """A changed collection is a move. Against a real server, a stale copy left
    behind is exactly the duplicate-in-a-shared-calendar failure this project
    exists to prevent."""
    target = _target(password)
    source = repo.get_source(configured, "tr-wrens")
    sync_source(configured, source, target, now=NOW, raw=FIXTURE.read_bytes())

    before = repo.event_states(configured, "tr-wrens")
    sample = next(iter(before.values()))
    staged_url = f"{BASE}/{USER}/{RUN}-staging/{sample.remote_id}.ics"
    assert call("GET", staged_url, password=password).status == 200

    repo.set_staging(configured, "tr-wrens", None)
    promoted = sync_source(
        configured, repo.get_source(configured, "tr-wrens"), target,
        now=NOW, raw=FIXTURE.read_bytes(),
    )

    assert promoted.moved > 0, "promotion moved nothing"
    after = repo.event_states(configured, "tr-wrens")
    assert {s.collection for s in after.values()} <= {f"{RUN}-games", f"{RUN}-practices"}

    # The staged copy is gone, not orphaned alongside the promoted one.
    assert call("GET", staged_url, password=password).status in (404, 410)


# --- the API and the calendar are one answer, not two -----------------------

API_TOKEN = "acceptance-bearer-token"


def _api_get(app, path):
    """One WSGI call, so the auth hook and the JSON body are both exercised."""
    from io import BytesIO, StringIO

    path, _, query = path.partition("?")
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = int(status.split()[0])

    body = b"".join(
        app(
            {
                "REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query,
                "SERVER_NAME": "localhost", "SERVER_PORT": "8731",
                "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.input": BytesIO(b""),
                "wsgi.errors": StringIO(), "wsgi.url_scheme": "http",
                "HTTP_AUTHORIZATION": f"Bearer {API_TOKEN}",
            },
            start_response,
        )
    )
    assert captured["status"] == 200, body
    return json.loads(body)


def test_the_api_serves_what_the_calendar_actually_holds(configured, tmp_path, password):
    """The one failure mode storing event content introduces, ruled out for real.

    "The API says 7pm, the calendar says 8pm" is what a second copy of the truth
    risks, and every other test of it compares against `.ics` files this process
    just wrote. This compares against what a real CalDAV server will hand a real
    calendar client — the same bytes a phone would sync.
    """
    from icalendar import Calendar

    from calsync.api import app as api_app

    source = repo.get_source(configured, "tr-wrens")
    report = sync_source(configured, source, _target(password), now=NOW,
                         raw=FIXTURE.read_bytes())
    assert report.created > 0, report.line()

    app = api_app.create_app(
        tmp_path / "calsync.db",
        secrets=SecretStore(environ={"CALSYNC_SECRET_API_TOKEN": API_TOKEN}),
        clock=lambda: NOW,
    )
    served = _api_get(app, "/v1/events?from=2026-03-01&to=2026-04-01")
    assert served["count"] > 0, "the API found nothing the sync had just written"

    states = repo.event_states(configured, "tr-wrens")
    for event in served["events"]:
        state = states[event["uid"]]
        fetched = call(
            "GET", f"{BASE}/{USER}/{state.collection}/{state.remote_id}.ics",
            password=password,
        )
        assert fetched.status == 200
        vevent = Calendar.from_ical(fetched.body.decode()).walk("VEVENT")[0]

        assert str(vevent["SUMMARY"]) == event["summary_rendered"]
        assert vevent.decoded("DTSTART") == datetime.fromisoformat(event["starts_at"])
        assert vevent.decoded("DTEND") == datetime.fromisoformat(event["ends_at"])
        if event["venue"]:
            assert str(vevent["LOCATION"]).startswith(
                event["venue"]["canonical_name"]
            )

    # And it says which source stands behind the answer, with a real success
    # timestamp from the poll that just ran. Whether `stale` flips is decided by
    # the clock rather than by the server, so `tests/test_api.py` pins that.
    health = served["sources"][0]
    assert health["id"] == "tr-wrens"
    assert health["last_success_at"] is not None
    assert health["last_error"] is None


# --- the configured path, not a hand-built one ------------------------------
#
# Everything above constructs its target from `BASE` and a password, which is
# how a deployment sat for days writing nothing while these were green:
# `settings.radicale_url` was wrong, and no test ever read it. These go through
# `targeting.build_target` — the function the poller actually calls — so the
# settings→target→server path is exercised rather than assumed.


@pytest.fixture
def configured_by_settings(tmp_path, password):
    """A database whose *settings* describe the live server."""
    from calsync.settings import set_setting

    conn = db.open_db(tmp_path / "configured.db")
    set_setting(conn, "radicale_url", BASE)
    set_setting(conn, "radicale_user", USER)
    set_setting(conn, "radicale_secret_ref", "radicale_password")
    set_setting(conn, "target_kind", "caldav")
    conn.commit()
    return conn


def _store():
    from calsync.secrets import SecretStore

    return SecretStore(path=Path("/nonexistent"),
                       environ={"CALSYNC_SECRET_RADICALE_PASSWORD": WRITER_PASSWORD})


def test_the_configured_target_can_reach_the_server(configured_by_settings):
    """`targeting.verify` against a real Radicale, not a stub.

    The unit tests for it drive a fake transport, which proves the branching and
    not that the probe it sends is one this server answers.
    """
    from calsync import targeting

    check = targeting.verify(configured_by_settings, _store())

    assert check.ok, [f"{f.label}: {f.detail}" for f in check.findings]


def test_a_wrong_url_in_settings_is_caught_by_the_check(configured_by_settings):
    """The original failure, reproduced against the real thing.

    Radicale is up and healthy throughout; only the setting is wrong. That is
    exactly the shape of the bug — a stack that looks fine and writes nothing.
    """
    from calsync import targeting
    from calsync.settings import set_setting

    set_setting(configured_by_settings, "radicale_url", "http://localhost:59999")

    check = targeting.verify(configured_by_settings, _store())

    assert not check.ok
    assert "localhost" in check.findings[0].detail


def test_a_sync_through_the_configured_target_writes_to_the_real_server(
    configured_by_settings, password
):
    """The whole path the poller takes, end to end.

    Nothing here names a URL: the target is built from settings, exactly as
    `cmd_sync` and the poll loop build it. If the configured address is wrong
    this fails, which is the coverage that was missing.
    """
    from calsync import targeting

    conn = configured_by_settings
    conn.executescript(
        f"""
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('mira', 'Mira', 'M', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz)
             VALUES ('a', 'mira', 'Wrens', 'soccer', 'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape, staging_collection)
             VALUES ('cfg', 'a', 'teamreach', 'feed', '{RUN}-configured');
        """
    )
    conn.commit()

    target = targeting.build_target(conn, secrets=_store())
    report = sync_source(conn, repo.get_source(conn, "cfg"), target,
                         now=NOW, raw=FIXTURE.read_bytes())

    assert report.status == "ok", report.line()
    assert report.created > 0

    state = next(iter(repo.event_states(conn, "cfg").values()))
    fetched = call("GET", f"{BASE}/{USER}/{RUN}-configured/{state.remote_id}.ics",
                   password=password)
    assert fetched.status == 200
    call("DELETE", f"{BASE}/{USER}/{RUN}-configured/", password=password)
