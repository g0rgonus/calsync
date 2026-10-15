"""The inbound API: a tool pushes one team's events and calsync writes them.

Driven through WSGI, like the read API's tests, so the key check, the JSON
errors and the status codes are what a real client sees. Nothing below the API
is faked: every write runs the real sync loop into an `ics_file` target, so
"on the calendar" is an assertion about files on disk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from icalendar import Calendar

from calsync import db, repo
from calsync.inbound import app as inbound_app
from calsync.inbound import events as inbound
from calsync.inbound import recurrence
from calsync.sync import sync_source
from calsync.targets import build

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
KEY = "csk_test-key-for-the-otters-swim-team-000000"
OTHER_KEY = "csk_test-key-for-somebody-elses-team-11111"
BASE = "/inbound/v1/sources/in-herons"

PRACTICE = {
    "type": "practice",
    "label": "Gold group swim",
    "start": "2026-10-20T17:45:00",
    "end": "2026-10-20T19:30:00",
    "location": "Wrenfield Aquatic Center",
    "rrule": "FREQ=WEEKLY;BYDAY=TU;COUNT=6",
    "notes": "Arrive 10–15 min early",
}


class Client:
    def __init__(self, app, key=KEY):
        self.app = app
        self.key = key

    def call(self, method, path, body=None, *, key=..., headers=None, raw=None):
        key = self.key if key is ... else key
        if raw is None:
            raw = b"" if body is None else json.dumps(body).encode()
        path, _, query = path.partition("?")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8732",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/json",
            "wsgi.input": BytesIO(raw),
            "wsgi.errors": StringIO(),
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "localhost:8732",
            "REMOTE_ADDR": "10.0.0.5",
        }
        if key is not None:
            environ["HTTP_X_CALSYNC_KEY"] = key
        for name, value in (headers or {}).items():
            environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, headers_, exc_info=None):
            captured["status"] = int(status.split()[0])
            captured["headers"] = {k.lower(): v for k, v in headers_}

        text = b"".join(self.app(environ, start_response)).decode()
        captured["json"] = json.loads(text) if text.strip() else None
        return captured

    def put(self, path, body, **kw):
        return self.call("PUT", path, body, **kw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "calsync.db"
    conn = db.open_db(path)
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('jesse', 'Jesse', 'J', 1), ('nadia', 'Nadia', 'N', 2);
        INSERT INTO activities (id, child_id, name, sport_id, tz,
                                alarm_game_min, alarm_practice_min)
             VALUES ('jesse-swim-herons', 'jesse', 'Herons', 'swimming',
                     'America/New_York', 90, 30),
                    ('nadia-soccer-wrens', 'nadia', 'Wrens', 'soccer',
                     'America/New_York', 90, 30);
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('in-herons', 'jesse-swim-herons', 'inbound', 'push'),
                    ('in-wrens', 'nadia-soccer-wrens', 'inbound', 'push');
        """
    )
    repo.set_inbound_key_hash(conn, "in-herons", inbound.hash_key(KEY))
    repo.set_inbound_key_hash(conn, "in-wrens", inbound.hash_key(OTHER_KEY))
    conn.close()
    return path


@pytest.fixture
def out(tmp_path):
    return tmp_path / "out"


@pytest.fixture
def log():
    return []


@pytest.fixture
def client(db_path, out, log):
    app = inbound_app.create_app(
        db_path, clock=lambda: NOW, write_target=build("ics_file", directory=out),
        log=log.append,
    )
    return Client(app)


def _files(out: Path) -> dict[str, Path]:
    return {p.stem: p for p in out.rglob("*.ics")}


def _vevent(path: Path):
    cal = Calendar.from_ical(path.read_bytes())
    return next(c for c in cal.walk() if c.name == "VEVENT")


# --- recurrence -------------------------------------------------------------------


def test_a_weekly_series_keeps_its_wall_clock_time_across_the_dst_change():
    zone = recurrence.ZoneInfo("America/New_York")
    rule = recurrence.parse("FREQ=WEEKLY;BYDAY=TU;UNTIL=20261111T045959Z")
    starts = recurrence.expand(rule, datetime(2026, 10, 20, 17, 45), zone)
    assert [s.date().isoformat() for s in starts] == [
        "2026-10-20", "2026-10-27", "2026-11-03", "2026-11-10"]
    assert {s.time().isoformat() for s in starts} == {"17:45:00"}
    utc = [s.replace(tzinfo=zone).astimezone(timezone.utc).hour for s in starts]
    assert utc == [21, 21, 22, 22]   # the instant moves; the pool deck does not


@pytest.mark.parametrize("rule, fragment", [
    ("FREQ=WEEKLY;BYDAY=TU", "exactly one of UNTIL or COUNT"),
    ("FREQ=WEEKLY;COUNT=3;UNTIL=20270101", "exactly one of UNTIL or COUNT"),
    ("FREQ=MONTHLY;COUNT=3", "not supported"),
    ("FREQ=WEEKLY;BYDAY=2TU;COUNT=3", "ordinals"),
    ("FREQ=WEEKLY;BYMONTH=1;COUNT=3", "unsupported rule part"),
])
def test_rules_outside_the_subset_are_refused_not_approximated(rule, fragment):
    with pytest.raises(recurrence.RuleError, match=fragment):
        recurrence.expand(recurrence.parse(rule), datetime(2026, 10, 20, 17, 45),
                          recurrence.ZoneInfo("America/New_York"))


def test_a_start_off_its_own_rule_is_refused():
    with pytest.raises(recurrence.RuleError, match="falls on a MO"):
        recurrence.expand(recurrence.parse("FREQ=WEEKLY;BYDAY=TU;COUNT=2"),
                          datetime(2026, 10, 19, 17, 45),
                          recurrence.ZoneInfo("America/New_York"))


# --- auth ---------------------------------------------------------------------------


def test_no_key_is_401_with_a_challenge_that_is_not_basic(client):
    r = client.put(BASE + "/events/tue-swim", PRACTICE, key=None)
    assert r["status"] == 401
    assert r["json"]["error"]["code"] == "bad_credential"
    challenge = r["headers"]["www-authenticate"]
    # A Basic challenge here would make a proxy-aware client resend the proxy's
    # credentials, which can never satisfy this one.
    assert "basic" not in challenge.lower()


def test_a_wrong_key_and_an_unknown_source_are_the_same_answer(client):
    wrong = client.get(BASE, key="csk_nope")
    unknown = client.get("/inbound/v1/sources/in-nobody", key=KEY)
    assert wrong["status"] == unknown["status"] == 401
    assert wrong["json"] == unknown["json"]


def test_a_key_opens_its_own_team_and_no_other(client):
    assert client.get(BASE).get("status") == 200
    r = client.put("/inbound/v1/sources/in-wrens/events/x", PRACTICE)
    assert r["status"] == 401


def test_the_proxys_basic_auth_is_ignored_in_both_directions(client):
    basic = {"Authorization": "Basic cGhvdG9jb2FjaDpzZWNyZXQ="}
    # Present beside the key: the key decides.
    assert client.get(BASE, headers=basic)["status"] == 200
    # Present instead of the key: it is not a calsync credential.
    assert client.get(BASE, key=None, headers=basic)["status"] == 401


def test_errors_are_json_even_for_a_route_that_does_not_exist(client):
    r = client.get("/inbound/v1/nowhere")
    assert r["status"] == 404
    assert r["json"]["error"]["code"] == "http_404"


# --- upsert -------------------------------------------------------------------------------


def test_a_series_is_written_as_one_event_per_occurrence(client, out):
    r = client.put(BASE + "/events/tue-swim", PRACTICE)
    assert r["status"] == 201, r["json"]
    assert r["json"]["sync"]["status"] == "ok"
    files = _files(out)
    assert len(files) == 6
    first = _vevent(files["in-herons.tue-swim.20261020@inbound.calsync"])
    # The title is the household's template, not anything the sender wrote.
    assert "Jesse" in str(first["SUMMARY"]) and "Gold group swim" in str(first["SUMMARY"])
    assert "Wrenfield Aquatic Center" in str(first["LOCATION"])


def test_a_repeated_put_is_a_no_op_all_the_way_down(client, out, db_path):
    first = client.put(BASE + "/events/tue-swim", PRACTICE)
    polls = _polls(db_path)
    again = client.put(BASE + "/events/tue-swim", PRACTICE)
    assert again["status"] == 200
    assert again["json"]["changed"] is False
    assert again["headers"]["etag"] == first["headers"]["etag"]
    assert _polls(db_path) == polls          # no sync ran at all
    assert len(_files(out)) == 6


def _polls(db_path) -> int:
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM poll_runs").fetchone()[0]
    finally:
        conn.close()


def test_a_changed_field_updates_the_same_events(client, out):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    moved = dict(PRACTICE, start="2026-10-20T18:00:00", end="2026-10-20T19:45:00")
    r = client.put(BASE + "/events/tue-swim", moved)
    assert r["status"] == 200 and r["json"]["changed"] is True
    files = _files(out)
    assert len(files) == 6                   # same uids: a new time is not a new event
    start = _vevent(files["in-herons.tue-swim.20261103@inbound.calsync"])["DTSTART"].dt
    assert start.astimezone(recurrence.ZoneInfo("America/New_York")).hour == 18


def test_if_match_refuses_a_stale_write(client):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    r = client.put(BASE + "/events/tue-swim", PRACTICE, headers={"If-Match": '"stale"'})
    assert r["status"] == 412
    assert r["json"]["error"]["current"]


@pytest.mark.parametrize("change, field", [
    ({"tz": "EDT"}, "tz"),
    ({"end": "2026-10-20T17:00:00"}, "end"),
    ({"rrule": "FREQ=WEEKLY;BYDAY=TU"}, "rrule"),
    ({"type": "meet"}, "type"),
    ({"title": "Jesse – Gold group swim"}, "title"),
    ({"calendar": "patrick"}, "calendar"),
])
def test_validation_names_the_field(client, out, change, field):
    r = client.put(BASE + "/events/tue-swim", dict(PRACTICE, **change))
    assert r["status"] == 422
    assert field in {e["field"] for e in r["json"]["error"]["errors"]}
    assert not _files(out)


def test_a_staged_source_writes_to_the_staging_calendar(client, out, db_path):
    conn = db.connect(db_path)
    repo.set_staging(conn, "in-herons", "onboarding")
    conn.close()
    r = client.put(BASE + "/events/tue-swim", PRACTICE)
    assert r["json"]["sync"]["staged_to"] == "onboarding"
    assert {p.parent.name for p in _files(out).values()} == {"onboarding"}


def test_a_paused_source_refuses_writes(client, db_path):
    conn = db.connect(db_path)
    repo.set_enabled(conn, "in-herons", False)
    conn.close()
    assert client.put(BASE + "/events/tue-swim", PRACTICE)["status"] == 409


def test_a_game_carries_its_opponent_into_the_title(client, out):
    game = {"type": "game", "start": "2026-10-24T09:00:00", "end": "2026-10-24T12:00:00",
            "opponent": "Ridgeway Sharks", "home": False, "location": "Ridgeway Natatorium"}
    assert client.put(BASE + "/events/meet-1", game)["status"] == 201
    summary = str(_vevent(_files(out)["in-herons.meet-1@inbound.calsync"])["SUMMARY"])
    assert "Ridgeway Sharks" in summary


def test_an_all_day_event_ends_on_its_last_day(client, out):
    day = {"type": "game", "start": "2026-11-14", "end": "2026-11-15", "label": "Invitational"}
    assert client.put(BASE + "/events/invitational", day)["status"] == 201
    ev = _vevent(_files(out)["in-herons.invitational@inbound.calsync"])
    assert ev["DTSTART"].dt.isoformat() == "2026-11-14"
    assert ev["DTEND"].dt.isoformat() == "2026-11-16"   # exclusive, per RFC 5545


def test_what_was_sent_comes_back(client):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    r = client.get(BASE + "/events/tue-swim")
    assert r["status"] == 200
    event = r["json"]["event"]
    assert event["label"] == "Gold group swim"
    assert event["notes"] == "Arrive 10–15 min early"
    assert len(r["json"]["occurrences"]) == 6
    listed = client.get(BASE + "/events")["json"]
    assert listed["count"] == 1


# --- occurrences --------------------------------------------------------------------------


def test_an_override_moves_one_occurrence_and_no_other(client, out):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    r = client.put(BASE + "/events/tue-swim/occurrences/2026-10-27",
                   {"start": "2026-10-27T18:30:00", "location": "Larkmoor Pool"})
    assert r["status"] == 200, r["json"]
    files = _files(out)
    moved = _vevent(files["in-herons.tue-swim.20261027@inbound.calsync"])
    other = _vevent(files["in-herons.tue-swim.20261103@inbound.calsync"])
    assert "Larkmoor Pool" in str(moved["LOCATION"])
    zone = recurrence.ZoneInfo("America/New_York")
    assert moved["DTSTART"].dt.astimezone(zone).strftime("%H:%M") == "18:30"
    assert moved["DTEND"].dt.astimezone(zone).strftime("%H:%M") == "20:15"  # duration kept
    assert "Wrenfield" in str(other["LOCATION"])


def test_an_occurrence_the_series_does_not_have_is_422(client):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    r = client.put(BASE + "/events/tue-swim/occurrences/2026-10-28", {"notes": "x"})
    assert r["status"] == 422
    assert r["json"]["error"]["code"] == "not_an_occurrence"


def test_cancelling_one_occurrence_is_idempotent_and_reversible(client, out):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    uid = "in-herons.tue-swim.20261103@inbound.calsync"
    path = BASE + "/events/tue-swim/occurrences/2026-11-03"
    assert client.delete(path)["status"] == 204
    assert uid not in _files(out) and len(_files(out)) == 5
    assert client.delete(path)["status"] == 204
    assert len(_files(out)) == 5
    # Putting it back reinstates it.
    assert client.put(path, {})["status"] == 200
    assert uid in _files(out)


def test_shortening_a_series_cancels_the_dropped_dates_without_tripping_the_guard(client, out):
    """Five dates vanish at once — more than the guard's count. They are not an
    absence, because the sender said so; nothing may hold them."""
    client.put(BASE + "/events/tue-swim", dict(PRACTICE, rrule="FREQ=WEEKLY;BYDAY=TU;COUNT=10"))
    assert len(_files(out)) == 10
    r = client.put(BASE + "/events/tue-swim", dict(PRACTICE, rrule="FREQ=WEEKLY;BYDAY=TU;COUNT=5"))
    assert r["json"]["sync"]["status"] == "ok", r["json"]["sync"]
    assert len(_files(out)) == 5


def test_moving_a_series_to_another_day_moves_every_event(client, out):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    client.put(BASE + "/events/tue-swim", dict(
        PRACTICE, start="2026-10-21T17:45:00", end="2026-10-21T19:30:00",
        rrule="FREQ=WEEKLY;BYDAY=WE;COUNT=6"))
    assert sorted(_files(out)) == sorted(
        f"in-herons.tue-swim.{d}@inbound.calsync"
        for d in ("20261021", "20261028", "20261104", "20261111", "20261118", "20261125"))


def test_an_override_orphaned_by_a_series_change_is_reported(client):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    client.put(BASE + "/events/tue-swim/occurrences/2026-11-24", {"notes": "relay day"})
    r = client.put(BASE + "/events/tue-swim", dict(PRACTICE, rrule="FREQ=WEEKLY;BYDAY=TU;COUNT=3"))
    assert r["json"]["orphaned_overrides"] == ["2026-11-24"]


# --- cancelling ------------------------------------------------------------------------------


def test_deleting_an_event_takes_every_occurrence_off(client, out):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    assert client.delete(BASE + "/events/tue-swim")["status"] == 204
    assert not _files(out)
    assert client.delete(BASE + "/events/tue-swim")["status"] == 204
    assert client.delete(BASE + "/events/never-sent")["status"] == 404
    # Still listed, as cancelled: the store never loses an item.
    assert client.get(BASE + "/events/tue-swim")["json"]["cancelled"] is True


def test_putting_a_cancelled_event_back_restores_it(client, out):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    client.delete(BASE + "/events/tue-swim")
    assert client.put(BASE + "/events/tue-swim", PRACTICE)["status"] == 200
    assert len(_files(out)) == 6


def test_the_log_never_carries_the_key_or_the_body(client, log):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    client.put(BASE + "/events/tue-swim", PRACTICE, key="csk_wrong")
    joined = "\n".join(log)
    assert "tue-swim" in joined
    assert KEY not in joined and "Wrenfield" not in joined


# --- the poller -------------------------------------------------------------------------------


def test_the_poller_reads_the_store_where_a_feed_would_be_fetched(client, db_path, out):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    conn = db.connect(db_path)
    source = repo.get_source(conn, "in-herons")

    def no_network(_url):
        raise AssertionError("an inbound source must never fetch")

    report = sync_source(conn, source, build("ics_file", directory=out), now=NOW,
                         fetcher=no_network)
    assert report.status == "ok"
    assert report.unchanged == 6
    conn.close()


def test_an_empty_store_beside_written_events_is_an_error_not_a_cancellation(
        client, db_path, out):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    conn = db.connect(db_path)
    conn.execute("DELETE FROM inbound_events")
    conn.commit()
    report = sync_source(conn, repo.get_source(conn, "in-herons"),
                         build("ics_file", directory=out), now=NOW)
    assert report.status == "error"
    assert len(_files(out)) == 6
    conn.close()


def test_the_gate_waits_for_a_push_not_for_a_game(client, db_path):
    conn = db.connect(db_path)
    source = repo.get_source(conn, "in-herons")
    before = sync_source(conn, source, None, now=NOW, dry_run=True)
    assert before.status == "ok" and not before.promotable
    conn.close()
    # Practices only, no game — and no location, which would ask a venue question.
    practice = {k: v for k, v in PRACTICE.items() if k != "location"}
    client.put(BASE + "/events/tue-swim", practice)
    conn = db.connect(db_path)
    after = sync_source(conn, source, None, now=NOW, dry_run=True)
    assert after.promotable
    conn.close()


# --- the description ----------------------------------------------------------------------------


def test_the_description_matches_the_route_table(client):
    app = client.app
    actual = {(r.method, r.rule.replace("<", "{").replace(">", "}")) for r in app.routes}
    described = {(r.method, r.path) for r in inbound_app.ROUTES}
    assert actual == described
    r = client.get("/inbound/v1", key=None)
    assert r["status"] == 200
    assert len(r["json"]["routes"]) == len(inbound_app.ROUTES)


def test_the_spec_describes_every_route_and_accepts_its_own_examples(client):
    """The OpenAPI document is generated, so it can only drift if the tables do —
    and its examples are run through the real validator, so a sample a tool
    copies from it is a sample calsync takes."""
    from calsync.inbound import contract
    spec = client.get("/inbound/v1/openapi.json", key=None,
                      headers={"X-Forwarded-Proto": "https",
                               "X-Forwarded-Host": "inbound.example.org"})
    assert spec["status"] == 200
    doc = spec["json"]
    assert doc["openapi"].startswith("3.1")
    assert doc["servers"] == [{"url": "https://inbound.example.org"}]
    ops = {(m.upper(), p) for p, item in doc["paths"].items() for m in item}
    assert ops == {(r.method, r.path) for r in contract.ROUTES}
    assert set(doc["components"]["schemas"]["Event"]["properties"]) == inbound.FIELDS
    for example in (contract.PRACTICE, contract.MEET, contract.WARMUP, contract.SESSION):
        inbound.validate_event(example, default_tz="America/New_York")


def test_the_examples_work_against_the_api_they_document(client, out):
    from calsync.inbound import contract
    assert client.put(BASE + "/events/tue-swim", contract.PRACTICE)["status"] == 201
    assert client.put(BASE + "/events/tue-swim/occurrences/2026-10-27",
                      contract.OVERRIDE)["status"] == 200
    assert client.put(BASE + "/events/harbor-invitational", contract.MEET)["status"] == 201
    assert client.put(BASE + "/events/harbor-invitational-s3-warmup",
                      contract.WARMUP)["status"] == 201
    assert client.put(BASE + "/events/harbor-invitational-s3",
                      contract.SESSION)["status"] == 201


def test_the_page_is_html_with_no_key_and_no_script(client):
    from io import BytesIO as _B, StringIO as _S
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/inbound/v1/docs", "QUERY_STRING": "",
               "SERVER_NAME": "localhost", "SERVER_PORT": "8732",
               "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.input": _B(b""), "wsgi.errors": _S(),
               "wsgi.url_scheme": "http", "HTTP_HOST": "<script>x</script>"}
    seen = {}

    def start_response(status, headers, exc_info=None):
        seen["status"] = int(status.split()[0])
        seen["headers"] = {k.lower(): v for k, v in headers}

    body = b"".join(client.app(environ, start_response)).decode()
    assert seen["status"] == 200
    assert seen["headers"]["content-type"].startswith("text/html")
    assert "default-src 'none'" in seen["headers"]["content-security-policy"]
    assert "<script" not in body                 # not even a reflected Host header
    assert "putOccurrence" in body and "possible_duplicate" in body
    assert KEY not in body


# --- hygiene -----------------------------------------------------------------------------------


def test_every_response_is_uncacheable_and_unsniffable(client):
    for r in (client.get(BASE), client.get(BASE, key="csk_wrong"),
              client.put(BASE + "/events/x", {"type": "nope"})):
        assert r["headers"].get("cache-control") == "no-store", r["status"]
        assert r["headers"].get("x-content-type-options") == "nosniff", r["status"]


def test_writes_are_throttled_per_team(db_path, out):
    ticks = iter(range(1000))
    app = inbound_app.create_app(
        db_path, clock=lambda: NOW, write_target=build("ics_file", directory=out),
        log=lambda _m: None, writes_per_minute=3, monotonic=lambda: next(ticks) * 0.1,
    )
    c = Client(app)
    single = {"type": "practice", "start": "2026-10-20T17:45:00", "end": "2026-10-20T19:00:00"}

    def on(day):
        return dict(single, start=f"2026-10-{day}T17:45:00", end=f"2026-10-{day}T19:00:00")

    statuses = [c.put(BASE + f"/events/e{i}", on(20 + i))["status"] for i in range(4)]
    assert statuses == [201, 201, 201, 429]
    # Another team's key is not slowed by this one's.
    other = Client(app, key=OTHER_KEY)
    assert other.put("/inbound/v1/sources/in-wrens/events/e1", single)["status"] == 201


def test_a_refused_key_is_logged_without_the_key(client, log):
    client.get(BASE, key="csk_guessing-at-this")
    assert any("refused" in line and "wrong key" in line for line in log)
    assert not any("csk_guessing" in line for line in log)


def test_the_unauthenticated_page_does_not_say_which_build_this_is(client):
    from calsync import __version__
    r = client.get("/inbound/v1", key=None)
    assert __version__ not in json.dumps(r["json"])



# --- finding the right id ----------------------------------------------------------------------


def test_a_new_id_for_an_event_already_sent_is_refused_as_a_duplicate(client, out):
    """The amnesiac sender: a fresh conversation, the same practice, a new id."""
    client.put(BASE + "/events/tue-swim", PRACTICE)
    again = dict(PRACTICE, start="2026-11-03T18:00:00", end="2026-11-03T19:30:00",
                 rrule="FREQ=WEEKLY;BYDAY=TU;COUNT=2")
    r = client.put(BASE + "/events/tuesday-practice", again)
    assert r["status"] == 409
    assert r["json"]["error"]["code"] == "possible_duplicate"
    assert r["json"]["error"]["duplicates"][0] == {
        "external_id": "tue-swim", "date": "2026-11-03"}
    assert len(_files(out)) == 6                       # nothing was written


def test_overlap_is_allowed_when_said_on_purpose_and_ignored_across_types(client):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    second = dict(PRACTICE, label="Dryland", rrule=None)
    second.pop("rrule")
    assert client.put(BASE + "/events/dryland", second)["status"] == 409
    r = client.put(BASE + "/events/dryland?allow_overlap=true", second)
    assert r["status"] == 201
    # A game at the same time is a different kind of thing, never a duplicate.
    game = {"type": "game", "start": "2026-10-20T18:00:00", "end": "2026-10-20T19:00:00",
            "opponent": "Sharks"}
    assert client.put(BASE + "/events/meet", game)["status"] == 201


def test_create_only_refuses_an_id_that_exists(client):
    first = client.put(BASE + "/events/tue-swim", PRACTICE, headers={"If-None-Match": "*"})
    assert first["status"] == 201
    again = client.put(BASE + "/events/tue-swim", PRACTICE, headers={"If-None-Match": "*"})
    assert again["status"] == 412
    assert again["json"]["error"]["code"] == "already_exists"


def test_the_list_narrows_to_a_window_of_dates(client):
    client.put(BASE + "/events/tue-swim", PRACTICE)
    client.put(BASE + "/events/invitational",
               {"type": "game", "start": "2026-12-12", "label": "Invitational"})
    r = client.get(BASE + "/events?from=2026-10-27&to=2026-10-27")
    assert r["status"] == 200
    assert [e["external_id"] for e in r["json"]["events"]] == ["tue-swim"]
    assert [o["date"] for o in r["json"]["events"][0]["occurrences"]] == ["2026-10-27"]
    assert client.get(BASE + "/events?from=soon")["status"] == 422



# --- review fixes ---------------------------------------------------------------------------------


def _racing_reads(monkeypatch):
    """Hold each read of an event until a second one arrives — which only
    happens if nothing serialises the writers. With serialisation the second
    never gets in, the barrier times out, and the first carries on alone."""
    import threading

    gate = threading.Barrier(2, timeout=0.5)
    real = repo.inbound_item

    def racing_read(conn, source_id, external_id):
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return real(conn, source_id, external_id)

    monkeypatch.setattr(repo, "inbound_item", racing_read)


def _at_once(*calls):
    import threading

    results = [None] * len(calls)

    def run(i, call):
        results[i] = call()["status"]

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(results)


def test_two_service_instances_on_one_database_cannot_both_win(db_path, out, monkeypatch):
    """No shared in-process lock here: two apps, as two replicas would be. Only
    SQLite's write lock, taken before the first read, stands between them."""
    def instance():
        return Client(inbound_app.create_app(
            db_path, clock=lambda: NOW, write_target=build("ics_file", directory=out),
            log=lambda _m: None))

    one, two = instance(), instance()
    tag = one.put(BASE + "/events/tue-swim", PRACTICE)["headers"]["etag"]
    _racing_reads(monkeypatch)
    statuses = _at_once(
        lambda: one.put(BASE + "/events/tue-swim", dict(PRACTICE, notes="a"),
                        headers={"If-Match": tag}),
        lambda: two.put(BASE + "/events/tue-swim", dict(PRACTICE, notes="b"),
                        headers={"If-Match": tag}),
    )
    assert statuses == [200, 412]


def test_two_new_ids_for_one_practice_at_once_make_one_event(client, out, monkeypatch):
    """The duplicate check reads the other events, then saves; two creates
    racing between those steps would each see nothing and both be written."""
    _racing_reads(monkeypatch)
    statuses = _at_once(
        lambda: client.put(BASE + "/events/tue-swim", PRACTICE),
        lambda: client.put(BASE + "/events/tuesday-practice", PRACTICE),
    )
    assert statuses == [201, 409]
    assert len(_files(out)) == 6


def test_two_writers_with_the_same_etag_cannot_both_win(client, monkeypatch):
    """Both read the event, both pass If-Match, the second silently replaces
    the first — unless the read-check-save is one step. The barrier holds each
    reader until the other arrives, which only happens if nothing serialises
    them; with the lock, the second never gets in and the barrier times out."""
    import threading

    first = client.put(BASE + "/events/tue-swim", PRACTICE)
    tag = first["headers"]["etag"]
    gate = threading.Barrier(2, timeout=0.5)
    real = repo.inbound_item

    def racing_read(conn, source_id, external_id):
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return real(conn, source_id, external_id)

    monkeypatch.setattr(repo, "inbound_item", racing_read)
    results = []

    def write(label):
        body = dict(PRACTICE, notes=label)
        results.append(client.put(BASE + "/events/tue-swim", body,
                                  headers={"If-Match": tag})["status"])

    threads = [threading.Thread(target=write, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 412]


def test_the_key_is_checked_before_the_body_is_read(client):
    # Unparseable, and no key. Parsing first would answer 400 bad_json.
    r = client.call("PUT", BASE + "/events/x", key=None, raw=b"{")
    assert r["status"] == 401


def test_a_weak_etag_from_a_compressing_proxy_still_matches(client):
    tag = client.put(BASE + "/events/tue-swim", PRACTICE)["headers"]["etag"]
    r = client.put(BASE + "/events/tue-swim", dict(PRACTICE, notes="changed"),
                   headers={"If-Match": "W/" + tag})
    assert r["status"] == 200


def test_the_log_names_the_real_peer_beside_a_claimed_address(client, log):
    client.get(BASE, key="csk_wrong", headers={"X-Forwarded-For": "203.0.113.9"})
    assert any("from 203.0.113.9 via 10.0.0.5" in line for line in log)
    client.get(BASE, key="csk_wrong")
    assert any(line.endswith("from 10.0.0.5") for line in log)


def test_a_read_marks_the_key_used_and_a_new_key_starts_over(client, db_path):
    conn = db.connect(db_path)
    assert repo.inbound_key_used_at(conn, "in-herons") is None
    client.get(BASE + "/events")
    used = repo.inbound_key_used_at(conn, "in-herons")
    assert used == NOW.isoformat()
    repo.set_inbound_key_hash(conn, "in-herons", inbound.hash_key("csk_new"))
    assert repo.inbound_key_used_at(conn, "in-herons") is None
    conn.close()


def test_a_busy_database_never_holds_up_a_read_for_the_last_used_stamp(client, db_path):
    import time as _time

    poller = db.connect(db_path)
    poller.execute("BEGIN IMMEDIATE")          # the poller, mid-write
    try:
        started = _time.monotonic()
        r = client.get(BASE)
        elapsed = _time.monotonic() - started
    finally:
        poller.rollback()
        poller.close()
    assert r["status"] == 200
    assert elapsed < 1.5, f"a read waited {elapsed:.1f}s on a timestamp"


# --- part_of: a meet replaced by its sessions (#51) ------------------------------------------

MEET = {"type": "game", "label": "Harbor Invitational", "start": "2026-11-14",
        "end": "2026-11-15", "location": "Ridgeway Natatorium"}
MEET_UID = "in-herons.harbor-inv@inbound.calsync"


def _session(n, start, end, **extra):
    return {"type": "game", "label": f"Harbor Inv · Session {n}",
            "start": f"2026-11-14T{start}:00", "end": f"2026-11-14T{end}:00",
            "part_of": "harbor-inv", **extra}


def test_a_session_replaces_its_meet_on_the_calendar(client, out):
    assert client.put(BASE + "/events/harbor-inv", MEET)["status"] == 201
    assert MEET_UID in _files(out)
    # Overlaps the all-day meet and is the same type: not a duplicate, a part.
    r = client.put(BASE + "/events/harbor-inv-s3", _session(3, "13:00", "16:30"))
    assert r["status"] == 201, r["json"]
    assert r["json"]["sync"]["status"] == "ok"
    files = _files(out)
    assert MEET_UID not in files
    assert "in-herons.harbor-inv-s3@inbound.calsync" in files


def test_the_meet_comes_back_when_every_session_is_withdrawn(client, out):
    client.put(BASE + "/events/harbor-inv", MEET)
    client.put(BASE + "/events/harbor-inv-s1", _session(1, "08:00", "11:30"))
    client.put(BASE + "/events/harbor-inv-s3", _session(3, "13:00", "16:30"))
    client.delete(BASE + "/events/harbor-inv-s1")
    assert MEET_UID not in _files(out)            # one session is still live
    client.delete(BASE + "/events/harbor-inv-s3")
    assert set(_files(out)) == {MEET_UID}          # the meet is still on
    # Still in the store and never marked cancelled: it was replaced, not deleted.
    assert client.get(BASE + "/events/harbor-inv")["json"]["cancelled"] is False


def test_a_warm_up_and_a_session_land_on_two_calendars_and_the_meet_goes(client, out):
    client.put(BASE + "/events/harbor-inv", MEET)
    warmup = {"type": "practice", "label": "Harbor Inv · S3 warm-up",
              "start": "2026-11-14T12:00:00", "end": "2026-11-14T12:50:00",
              "part_of": "harbor-inv"}
    assert client.put(BASE + "/events/harbor-inv-s3-warmup", warmup)["status"] == 201
    assert client.put(BASE + "/events/harbor-inv-s3",
                      _session(3, "13:00", "16:30"))["status"] == 201
    files = _files(out)
    assert MEET_UID not in files
    assert {files[u].parent.name for u in files} == {"games", "practices"}
    assert len(files) == 2


def test_two_sessions_of_one_meet_at_the_same_time_are_still_a_duplicate(client):
    client.put(BASE + "/events/harbor-inv", MEET)
    client.put(BASE + "/events/harbor-inv-s3", _session(3, "13:00", "16:30"))
    r = client.put(BASE + "/events/harbor-inv-session-3", _session(3, "13:00", "16:30"))
    assert r["status"] == 409
    assert r["json"]["error"]["duplicates"][0]["external_id"] == "harbor-inv-s3"


_S3 = _session(3, "13:00", "16:30")


@pytest.mark.parametrize("already_sent, external_id, body, code", [
    # The meet has not been sent.
    ({}, "harbor-inv-s3", _S3, "no_such_event"),
    # An event naming itself.
    ({"harbor-inv": MEET}, "harbor-inv", dict(MEET, part_of="harbor-inv"), "self"),
    # A part of a part: one level only.
    ({"harbor-inv": MEET, "harbor-inv-s3": _S3}, "harbor-inv-s3-heat",
     dict(_session(9, "17:00", "18:00"), part_of="harbor-inv-s3"), "too_deep"),
    # A meet that has parts becoming a part of something else.
    ({"other-meet": dict(MEET, start="2026-12-05", end="2026-12-05"),
      "harbor-inv": MEET, "harbor-inv-s3": _S3},
     "harbor-inv", dict(MEET, part_of="other-meet"), "has_parts"),
])
def test_part_of_is_refused_where_it_would_not_mean_anything(client, already_sent,
                                                              external_id, body, code):
    for eid, sent in already_sent.items():
        assert client.put(BASE + f"/events/{eid}", sent)["status"] == 201
    r = client.put(BASE + f"/events/{external_id}", body)
    assert r["status"] == 422, r["json"]
    assert [e["code"] for e in r["json"]["error"]["errors"]] == [code]


def test_part_of_must_look_like_an_id(client):
    r = client.put(BASE + "/events/harbor-inv-s3", dict(_session(3, "13:00", "16:30"),
                                                         part_of="../meet"))
    assert r["status"] == 422
    assert r["json"]["error"]["errors"][0]["field"] == "part_of"


def test_many_meets_replaced_in_one_poll_never_trip_the_guard(client, db_path, out):
    """Five placeholders leave the calendar at once — more than the guard's
    count. They are removals the sender asked for, not absences."""
    for n in range(5):
        day = f"2026-11-{14 + n:02d}"
        client.put(BASE + f"/events/meet-{n}", dict(MEET, start=day, end=day))
    assert len(_files(out)) == 5
    conn = db.connect(db_path)
    for n in range(5):
        day = f"2026-11-{14 + n:02d}"
        state = {"event": inbound.validate_event(
            dict(_session(1, "08:00", "11:00"), start=f"{day}T08:00:00",
                 end=f"{day}T11:00:00", part_of=f"meet-{n}"),
            default_tz="America/New_York"), "overrides": {}, "retired": []}
        repo.save_inbound_item(conn, "in-herons", f"meet-{n}-s1", state=state,
                               cancelled=False, etag="x", now=NOW.isoformat())
    conn.commit()
    report = sync_source(conn, repo.get_source(conn, "in-herons"),
                         build("ics_file", directory=out), now=NOW)
    conn.close()
    assert report.status == "ok", report.line()
    assert report.called_off == 5 and report.created == 5
    assert not any(uid.startswith("in-herons.meet-") and "-s1" not in uid
                   for uid in _files(out))


def test_the_api_says_a_replaced_meet_is_not_on_the_calendar(client):
    """It was never cancelled, so `cancelled` stays false — but a client that
    lists before it writes has to be able to tell it is not showing."""
    client.put(BASE + "/events/harbor-inv", MEET)
    client.put(BASE + "/events/harbor-inv-s3", _session(3, "13:00", "16:30"))
    meet = client.get(BASE + "/events/harbor-inv")["json"]
    assert meet["cancelled"] is False
    assert meet["superseded_by"] == ["harbor-inv-s3"]
    assert meet["on_calendar"] is False
    listed = {e["external_id"]: e for e in client.get(BASE + "/events")["json"]["events"]}
    assert listed["harbor-inv"]["on_calendar"] is False
    assert listed["harbor-inv-s3"]["on_calendar"] is True
    client.delete(BASE + "/events/harbor-inv-s3")
    back = client.get(BASE + "/events/harbor-inv")["json"]
    assert back["superseded_by"] == [] and back["on_calendar"] is True


def test_one_session_replaces_every_day_of_the_meet(client, out):
    """Documented behaviour: the placeholder is one multi-day event, so a
    Saturday-only swimmer has nothing on Friday or Sunday."""
    three_days = dict(MEET, start="2026-11-13", end="2026-11-15")
    client.put(BASE + "/events/harbor-inv", three_days)
    client.put(BASE + "/events/harbor-inv-s3", _session(3, "13:00", "16:30"))
    assert set(_files(out)) == {"in-herons.harbor-inv-s3@inbound.calsync"}


def test_a_replaced_meet_is_not_what_a_new_event_duplicates(client):
    client.put(BASE + "/events/harbor-inv", MEET)                      # Sat–Sun
    client.put(BASE + "/events/harbor-inv-s3", _session(3, "13:00", "16:30"))
    # A game on Sunday, not part of the meet. It overlaps only the hidden
    # placeholder, which is on no calendar.
    sunday = {"type": "game", "start": "2026-11-15T10:00:00",
              "end": "2026-11-15T11:00:00", "opponent": "Ridgeway Sharks"}
    assert client.put(BASE + "/events/scrimmage", sunday)["status"] == 201
    # Overlapping a session that *is* on the calendar still counts.
    clash = dict(sunday, start="2026-11-14T14:00:00", end="2026-11-14T15:00:00")
    r = client.put(BASE + "/events/scrimmage-2", clash)
    assert r["status"] == 409
    assert r["json"]["error"]["duplicates"][0]["external_id"] == "harbor-inv-s3"


def test_a_part_of_a_cancelled_meet_is_written_with_a_warning(client, out):
    client.put(BASE + "/events/harbor-inv", MEET)
    client.delete(BASE + "/events/harbor-inv")
    r = client.put(BASE + "/events/harbor-inv-s3", _session(3, "13:00", "16:30"))
    assert r["status"] == 201
    assert len(r["json"]["warnings"]) == 1 and "cancelled" in r["json"]["warnings"][0]
    assert set(_files(out)) == {"in-herons.harbor-inv-s3@inbound.calsync"}
    clean = client.put(BASE + "/events/harbor-inv-s3",
                       _session(3, "13:00", "16:45"))["json"]
    assert len(clean["warnings"]) == 1            # still true on every write
