"""The read API.

Driven through WSGI rather than through the route functions, so the auth hook,
the JSON content type and the error responses are all exercised — calling a
handler directly would skip every one of them.

Nothing is faked below the API itself: these run a real sync against a fixture
into a real `event_content` table and then read it back, because the assertion
that matters is that what an agent is told matches what was written to the
calendar.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path

import pytest

from calsync import db, repo
from calsync.api import app as api_app
from calsync.secrets import SecretError, SecretStore
from calsync.settings import set_setting
from calsync.sync import sync_source
from calsync.targets import build

FIXTURE = Path(__file__).parent / "fixtures" / "player360_sample.ics"
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
TOKEN = "s3cret-bearer-token"

#: The fixture's season runs into late August, past the API's default fortnight.
#: Anything asserting on event content has to ask for the span it lives in, or
#: it asserts against the one event that happens to fall inside the default.
SEASON = "/v1/events?from=2026-07-20&to=2026-09-01"


class Client:
    def __init__(self, app, token=TOKEN):
        self.app = app
        self.token = token

    def get(self, path, *, token=...):
        token = self.token if token is ... else token
        path, _, query = path.partition("?")
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8731",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.input": BytesIO(b""),
            "wsgi.errors": StringIO(),
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "localhost:8731",
        }
        if token is not None:
            environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"

        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(headers)

        body = b"".join(self.app(environ, start_response)).decode("utf-8", "replace")
        captured["body"] = body
        try:
            captured["json"] = json.loads(body)
        except ValueError:
            captured["json"] = None
        return captured


@pytest.fixture
def secrets(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"api_token": TOKEN}))
    path.chmod(0o600)
    return SecretStore(path=path, environ={})


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "calsync.db"
    conn = db.open_db(path)
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('jesse', 'Jesse', 'J', 1);
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('nadia', 'Nadia', 'N', 2);
        INSERT INTO activities (id, child_id, name, sport_id, official_name,
                                league, age_group, tz)
             VALUES ('jesse-soccer-vanguard', 'jesse', 'Vanguard', 'soccer', 'U10PL',
                     'PSL', 'U10', 'America/New_York');
        INSERT INTO sources (id, activity_id, kind, shape, tier)
             VALUES ('p360-jesse-vanguard', 'jesse-soccer-vanguard', 'player360', 'feed', 2);
        """
    )
    conn.commit()

    source = repo.list_sources(conn)[0]
    target = build("ics_file", directory=tmp_path / "out")
    sync_source(conn, source, target, now=NOW, raw=FIXTURE.read_bytes())
    conn.close()
    return path


@pytest.fixture
def client(db_path, secrets):
    return Client(api_app.create_app(db_path, secrets=secrets, clock=lambda: NOW))


# --- the credential ---------------------------------------------------------


def test_it_will_not_start_without_a_token(db_path, tmp_path):
    """A read API for a family's schedule that comes up unusable is one nobody
    notices is broken."""
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    empty.chmod(0o600)

    with pytest.raises(SecretError):
        api_app.create_app(db_path, secrets=SecretStore(path=empty, environ={}))


def test_a_deployment_can_name_its_own_secret(db_path, tmp_path):
    store = tmp_path / "other.json"
    store.write_text(json.dumps({"hermes_token": TOKEN}))
    store.chmod(0o600)

    conn = db.connect(db_path)
    set_setting(conn, "api_token_ref", "hermes_token")
    conn.close()

    app = api_app.create_app(
        db_path, secrets=SecretStore(path=store, environ={}), clock=lambda: NOW
    )
    assert Client(app).get("/v1/events")["status"] == 200


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_a_read_without_the_right_token_is_refused(client, token):
    reply = client.get("/v1/events", token=token)

    assert reply["status"] == 401
    assert reply["json"]["error"]["code"] in {"no_credential", "bad_credential"}
    assert TOKEN not in reply["body"]


def test_a_refusal_does_not_leak_the_schedule(client):
    reply = client.get("/v1/events", token="wrong-token")

    assert "Jesse" not in reply["body"]
    assert "Thistledown" not in reply["body"]


# --- what an agent gets that a calendar read cannot give it -----------------


def test_identity_comes_out_as_fields_not_as_a_parsed_title(client):
    """The argument the whole API rests on (docs/API.md).

    An agent handed a CalDAV account would have to pull "Jesse ⚽️ vs Harbour FC"
    back apart into a child and an opponent — reverse-engineering a string we
    generated, and re-breaking every time the convention changed.
    """
    reply = client.get(SEASON)
    assert reply["status"] == 200
    assert reply["headers"]["Content-Type"] == "application/json"

    fixture = next(e for e in reply["json"]["events"] if e["opponent"])
    assert fixture["child"] == {"id": "jesse", "name": "Jesse"}
    assert fixture["activity"]["id"] == "jesse-soccer-vanguard"
    assert fixture["activity"]["sport"] == "soccer"
    assert fixture["opponent"] == "Harbour FC"
    assert fixture["kind"] == "game"
    assert fixture["venue"]["raw"].startswith("Thistledown Park")
    assert fixture["venue"]["address"] == "1009 Thistledown Rd, Marbury NX 40114"


def test_the_title_is_rendered_now_not_read_back(client, db_path):
    """Change the convention and the API changes with it, with no re-fetch.

    This is the property a stored `summary` column would destroy, and the reason
    `summary_rendered` is documented as never-parse.
    """
    before = client.get(SEASON)["json"]["events"][0]["summary_rendered"]

    conn = db.connect(db_path)
    set_setting(conn, "title_template", "{emoji} {kids} :: {detail}")
    conn.close()

    after = client.get(SEASON)["json"]["events"][0]["summary_rendered"]

    assert " :: " in after
    assert after != before


def test_no_coordinates_are_served(client):
    """`venues` is the only table that holds a pin, and this is not it."""
    reply = client.get(SEASON)

    assert "lat" not in reply["body"]
    assert "lon" not in reply["body"]


def test_home_stays_unknown_rather_than_becoming_a_guess(client):
    """Player360 phrases every fixture as "vs", so null has to survive the trip."""
    fixture = next(e for e in client.get(SEASON)["json"]["events"] if e["is_game"])

    assert fixture["home"] is None


def test_a_venue_pin_confirmed_later_shows_against_events_written_earlier(
    client, db_path
):
    """Resolved at read time, so confirming a pin does not need a re-write."""
    assert all(
        not e["venue"]["pin_confirmed"]
        for e in client.get(SEASON)["json"]["events"]
        if e["venue"]
    )

    conn = db.connect(db_path)
    venue_id = repo.upsert_venue(conn, name="Thistledown Park", pin_confirmed=True)
    repo.add_venue_alias(conn, venue_id, "Thistledown Park")
    conn.close()

    events = client.get(SEASON)["json"]["events"]
    thistledown = [e for e in events if e["venue"] and "Thistledown" in e["venue"]["raw"]]
    assert thistledown
    assert all(e["venue"]["pin_confirmed"] for e in thistledown)
    assert all(e["venue"]["id"] == venue_id for e in thistledown)


def test_every_field_says_who_owns_it(client):
    """Uniform today — one feed contributes everything — but per-field anyway.

    An amendment holding a single field at tier 1 (docs/MATRIX.md §4) should
    widen what this says, not change its shape, so a client written now keeps
    working.
    """
    event = client.get(SEASON)["json"]["events"][0]

    assert set(event["resolution"]) >= {"venue", "starts_at", "opponent"}
    venue = event["resolution"]["venue"]
    assert venue["source_id"] == "p360-jesse-vanguard"
    assert venue["tier"] == 2
    assert venue["observed_at"].startswith("2026-07-20")


# --- staleness --------------------------------------------------------------


def test_a_stale_source_is_named_rather_than_served_silently(client, db_path):
    """`digest.py` names unreadable sources for the reason this does.

    A schedule answer that quietly omits its own uncertainty reads as current,
    and "the calendar says 8pm, the API said 7pm" is the one failure a stored
    copy introduces.
    """
    fresh = client.get("/v1/events")["json"]["sources"]
    assert fresh[0]["id"] == "p360-jesse-vanguard"
    assert fresh[0]["stale"] is False

    conn = db.connect(db_path)
    conn.execute("UPDATE sources SET last_success_at = '2026-01-01 00:00:00'")
    conn.commit()
    conn.close()

    stale = client.get("/v1/events")["json"]["sources"]
    assert stale[0]["stale"] is True


def test_a_failing_source_reports_its_error(client, db_path):
    conn = db.connect(db_path)
    repo.record_source_error(conn, "p360-jesse-vanguard", "feed returned 403")
    conn.commit()
    conn.close()

    assert (
        client.get("/v1/events")["json"]["sources"][0]["last_error"]
        == "feed returned 403"
    )


# --- bounds -----------------------------------------------------------------


def test_the_window_defaults_to_a_fortnight(client):
    reply = client.get("/v1/events")["json"]

    span = datetime.fromisoformat(reply["to"]) - datetime.fromisoformat(reply["from"])
    assert span == timedelta(days=14)


def test_a_narrower_window_returns_fewer_events(client):
    season = client.get(SEASON)["json"]["count"]
    fortnight = client.get("/v1/events")["json"]["count"]

    assert season > fortnight > 0


def test_asking_for_more_than_is_ever_written_is_refused(client):
    reply = client.get("/v1/events?from=2020-01-01&to=2030-01-01")

    assert reply["status"] == 400
    assert reply["json"]["error"]["code"] == "window_too_wide"


def test_reading_below_retention_says_so_instead_of_returning_empty(client):
    """Silently answering "nothing on" for a day that was simply pruned is the
    one wrong answer this response can give."""
    reply = client.get("/v1/events?from=2026-06-01&to=2026-07-25")["json"]

    assert "retained_from" in reply
    assert reply["from"] == reply["retained_from"]
    assert "2026-07-13" in reply["from"]


def test_a_naive_bound_is_read_as_utc_not_as_local(client):
    """An unmarked bound shifting the window by a day is the same class of bug
    `Event.__post_init__` refuses outright."""
    reply = client.get("/v1/events?from=2026-07-20T00:00:00")["json"]

    assert reply["from"].endswith("+00:00")


def test_an_unparseable_bound_is_refused_with_a_reason(client):
    reply = client.get("/v1/events?from=next+tuesday")

    assert reply["status"] == 400
    assert reply["json"]["error"]["code"] == "bad_datetime"
    assert reply["json"]["error"]["got"] == "next tuesday"


def test_a_backwards_window_is_refused(client):
    reply = client.get("/v1/events?from=2026-07-25&to=2026-07-20")

    assert reply["status"] == 400
    assert reply["json"]["error"]["code"] == "empty_window"


# --- filters and single reads -----------------------------------------------


def test_filtering_by_a_child_with_no_events_returns_none(client):
    assert client.get("/v1/events?child=nadia")["json"]["count"] == 0
    assert client.get("/v1/events?child=jesse")["json"]["count"] > 0


def test_one_event_by_uid(client):
    listed = client.get(SEASON)["json"]["events"][0]

    reply = client.get(f"/v1/events/{listed['uid']}")

    assert reply["status"] == 200
    assert reply["json"]["event"] == listed


def test_an_unknown_uid_is_a_404_that_explains_itself(client):
    reply = client.get("/v1/events/not-an-event")

    assert reply["status"] == 404
    assert reply["json"]["error"]["code"] == "unknown_event"


def test_a_cancelled_event_is_still_readable(client, db_path):
    """A tombstone nobody can render is not much of a tombstone."""
    uid = client.get(SEASON)["json"]["events"][0]["uid"]

    conn = db.connect(db_path)
    repo.mark_event_cancelled(conn, uid)
    conn.commit()
    conn.close()

    reply = client.get(f"/v1/events/{uid}")

    assert reply["status"] == 200
    assert reply["json"]["event"]["cancelled"] is True


# --- it agrees with the calendar --------------------------------------------


def test_what_the_api_says_is_what_was_written(client, tmp_path):
    """The whole staleness argument, as an assertion.

    Content is recorded behind the same barrier as placement, after the target
    accepted the write, so the two cannot drift. Compare the served times and
    titles against the `.ics` files the same sync produced.
    """
    written = {}
    for path in (tmp_path / "out").rglob("*.ics"):
        text = path.read_text()
        fields = dict(
            line.split(":", 1)
            for line in text.splitlines()
            if line.startswith(("UID:", "SUMMARY:", "LOCATION:"))
        )
        written[fields["UID"].strip()] = fields

    assert written

    for event in client.get(SEASON)["json"]["events"]:
        vevent = written[event["uid"]]
        assert event["summary_rendered"] == vevent["SUMMARY"].strip()
        if event["venue"]:
            assert vevent["LOCATION"].strip().startswith(
                event["venue"]["canonical_name"]
            )


# --- answering a question calsync asked -------------------------------------
#
# The write half, and it writes nothing anybody can see. An answer is stored for
# a human to approve in the console; there is no parameter on this endpoint that
# applies one, which is the review gate made structural rather than conventional.


def _post(client, path, body, *, token=TOKEN):
    from io import BytesIO, StringIO

    raw = json.dumps(body).encode()
    environ = {
        "REQUEST_METHOD": "POST", "PATH_INFO": path, "QUERY_STRING": "",
        "SERVER_NAME": "localhost", "SERVER_PORT": "8731",
        "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.input": BytesIO(raw),
        "wsgi.errors": StringIO(), "wsgi.url_scheme": "http",
        "HTTP_HOST": "localhost:8731", "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(raw)),
    }
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = int(status.split()[0])

    body_out = b"".join(client.app(environ, start_response)).decode()
    captured["body"] = body_out
    try:
        captured["json"] = json.loads(body_out)
    except ValueError:
        captured["json"] = None
    return captured


@pytest.fixture
def asked(db_path):
    """One dispatched question, as if the poller had posted it."""
    conn = db.connect(db_path)
    repo.record_task(
        conn, task_id="task_abc123", source_id="p360-jesse-vanguard",
        kind="unidentified", type="resolve_activity",
        context=("Ember vs Otters",), candidates=("Otters", "Ember"),
        dispatched_at="2026-07-20T12:00:00+00:00",
    )
    conn.commit()
    conn.close()
    return "task_abc123"


def test_an_answer_is_stored_and_explicitly_not_applied(client, asked, db_path):
    reply = _post(client, f"/v1/tasks/{asked}/result",
                  {"answer": {"alias": "Otters"}, "answered_by": "hermes/1.4",
                   "rationale": "Otters appears on both sides"})

    assert reply["status"] == 200
    assert reply["json"]["applied"] is False
    assert reply["json"]["state"] == "answered"

    conn = db.connect(db_path)
    assert repo.get_task(conn, asked).state == repo.ANSWERED
    # The gate: nothing was written as configuration.
    assert conn.execute("SELECT COUNT(*) n FROM activity_aliases").fetchone()["n"] == 0


def test_answering_a_question_nobody_asked_is_refused(client):
    """Rows are written when a question is dispatched, so this is the check that
    stops anything holding the token from inventing work for a human."""
    reply = _post(client, "/v1/tasks/task_invented/result",
                  {"answer": {"alias": "Whoever"}, "answered_by": "hermes"})

    assert reply["status"] == 404
    assert reply["json"]["error"]["code"] == "unknown_task"


def test_an_answer_needs_to_say_what_produced_it(client, asked):
    """A bad answer has to be traceable to a prompt weeks later."""
    reply = _post(client, f"/v1/tasks/{asked}/result", {"answer": {"alias": "x"}})

    assert reply["status"] == 422
    assert reply["json"]["error"]["code"] == "no_attribution"


def test_a_malformed_answer_is_refused_with_the_shape_it_wanted(client, asked):
    """Refused on the way in, not at approval time.

    The alternative puts the error in front of the one person who cannot fix it.
    """
    reply = _post(client, f"/v1/tasks/{asked}/result",
                  {"answer": {"wrong": "shape"}, "answered_by": "hermes"})

    assert reply["status"] == 422
    assert reply["json"]["error"]["code"] == "bad_answer"
    assert "alias" in json.dumps(reply["json"]["error"]["expected"])


def test_re_answering_a_decided_question_is_refused(client, asked, db_path):
    """Answering again would quietly reopen a decision somebody has taken."""
    conn = db.connect(db_path)
    repo.decide_task(conn, asked, repo.APPROVED)
    conn.commit()
    conn.close()

    reply = _post(client, f"/v1/tasks/{asked}/result",
                  {"answer": {"alias": "Otters"}, "answered_by": "hermes"})

    assert reply["status"] == 409
    assert reply["json"]["error"]["code"] == "already_decided"


def test_answering_without_the_token_is_refused(client, asked):
    reply = _post(client, f"/v1/tasks/{asked}/result",
                  {"answer": {"alias": "Otters"}, "answered_by": "hermes"},
                  token="wrong")

    assert reply["status"] == 401


def test_there_is_no_way_to_approve_through_the_api(client, asked, db_path):
    """The gate, stated as an assertion.

    Approving is a console action. Anything an agent sends here lands in the
    queue whatever it claims about itself.
    """
    _post(client, f"/v1/tasks/{asked}/result",
          {"answer": {"alias": "Otters"}, "answered_by": "hermes",
           "state": "approved", "approved": True, "apply": True})

    conn = db.connect(db_path)
    assert repo.get_task(conn, asked).state == repo.ANSWERED
    assert conn.execute("SELECT COUNT(*) n FROM activity_aliases").fetchone()["n"] == 0


# --- the contract -----------------------------------------------------------
#
# Hermes checks this before operating, so the one thing it must never do is
# describe an API that is not there. It is generated from `contract.py` and held
# against the app's own routes here.


def test_the_contract_and_the_routes_describe_each_other(client):
    """Both directions, which is the whole point.

    A hand-maintained page describing an HTTP API drifts from the API on the
    first busy afternoon, and a contract that lies is worse than no contract —
    an agent that trusts it fails in ways nobody can reproduce.
    """
    from calsync.api import contract

    served = {
        (r.method, r.rule) for r in client.app.routes
        # The catch-all is a fallback, not an endpoint anybody calls on purpose.
        if r.rule != "/v1/<path:path>"
    }
    documented = set(contract.ENDPOINTS)

    assert served - documented == set(), "routes the contract does not mention"
    assert documented - served == set(), "contract describes routes that do not exist"


def test_the_contract_is_served_and_names_what_it_serves(client):
    reply = client.get("/v1")

    assert reply["status"] == 200
    paths = {e["path"] for e in reply["json"]["endpoints"]}
    assert "/v1/events" in paths
    assert reply["json"]["contract_version"]


def test_the_contract_names_the_running_build(client):
    """Distinct from `contract_version`, and both are needed.

    `contract_version` moves when a client could notice a shape change; this
    moves whenever anything does. An agent debugging a deployment that ships on
    moving image tags needs the second, and there is no other way to ask a
    running calsync what it is.
    """
    import calsync

    body = client.get("/v1")["json"]
    assert body["version"] == calsync.__version__
    assert body["contract_version"] != body["version"], (
        "the contract's shape and the build are different questions")


def test_the_contract_carries_the_answer_shapes(client):
    """So a client corrects itself from the contract, not from a rejection."""
    shapes = client.get("/v1")["json"]["answer_shapes"]

    assert set(shapes) == {"resolve_activity", "classify_kind", "normalize_venue"}
    assert shapes["resolve_activity"]["answer"] == {"alias": "Otters"}


def test_the_contract_says_what_is_not_built(client):
    """Half of docs/API.md is design with no code behind it.

    An agent that reads the document and not the contract would try to POST a
    proposal; this is where it learns not to, and why.
    """
    missing = {n["path"]: n["reason"] for n in client.get("/v1")["json"]["not_implemented"]}

    assert "/v1/proposals" in missing
    assert "not built" in missing["/v1/proposals"].casefold()


def test_a_specified_but_unbuilt_endpoint_says_so_rather_than_404ing(client):
    """A bare 404 reads as a typo, and an agent will retry variations of it."""
    reply = _post(client, "/v1/proposals", {"event": {}})

    assert reply["status"] == 501
    assert reply["json"]["error"]["code"] == "not_implemented"
    assert reply["json"]["error"]["contract"] == "GET /v1"


def test_an_endpoint_nobody_ever_specified_is_a_plain_404(client):
    """Distinct from the above: "not yet" and "never" are different answers."""
    reply = client.get("/v1/wat")

    assert reply["status"] == 404
    assert reply["json"]["error"]["code"] == "no_such_endpoint"


def test_the_catch_all_does_not_shadow_a_real_route(client):
    """Bottle matches in definition order, so this is an ordering bug waiting
    to happen — it already happened once."""
    assert client.get(SEASON)["status"] == 200
    assert client.get("/v1/events/nope")["status"] == 404
    assert client.get("/v1/events/nope")["json"]["error"]["code"] == "unknown_event"


def test_reading_the_contract_still_needs_the_token(client):
    """It describes a private system, and the auth hook covers every route."""
    assert client.get("/v1", token=None)["status"] == 401


# --- the review queue -------------------------------------------------------
#
# Counts for something ambient — a menu bar — so that events sitting in the
# enrichment calendar are visible without anybody remembering to open the
# console.


def _hold(db_path, count, collection="enrichment"):
    """Move `count` synced events into the enrichment calendar."""
    conn = db.connect(db_path)
    uids = [r["uid"] for r in conn.execute(
        "SELECT uid FROM event_state ORDER BY uid LIMIT ?", (count,))]
    conn.executemany(
        "UPDATE event_state SET collection = ? WHERE uid = ?",
        [(collection, uid) for uid in uids])
    conn.commit()
    conn.close()
    return uids


def test_a_quiet_queue_reports_zero_rather_than_omitting_the_fields(client):
    body = client.get("/v1/review")["json"]
    assert body["needs_attention"] == 0
    assert body["held_events"] == 0
    assert body["sources"] == []
    assert body["answers_awaiting_decision"] == 0
    assert body["upstream_edits"] == 0


def test_held_events_are_counted_and_attributed_to_their_activity(client, db_path):
    _hold(db_path, 3)
    body = client.get("/v1/review")["json"]
    assert body["held_events"] == 3
    assert body["needs_attention"] == 3
    assert [s["source_id"] for s in body["sources"]] == ["p360-jesse-vanguard"]
    assert body["sources"][0]["held_events"] == 3
    assert body["sources"][0]["activity"]["name"] == "Vanguard"


def test_a_source_with_nothing_held_is_left_out_entirely(client, db_path):
    _hold(db_path, 1)
    body = client.get("/v1/review")["json"]
    assert all(s["held_events"] > 0 for s in body["sources"])


def test_the_count_follows_the_calendar_not_a_fresh_parse(client, db_path):
    """The number is what is actually in that calendar right now.

    `repo.events_in_collection` is the console's own definition, and using it
    here is the point: a digest, a console and an API disagreeing about how many
    events are waiting would each be a different wrong answer.
    """
    _hold(db_path, 2)
    assert client.get("/v1/review")["json"]["held_events"] == 2

    conn = db.connect(db_path)
    conn.execute("UPDATE event_state SET collection = 'games' "
                 "WHERE collection = 'enrichment'")
    conn.commit()
    conn.close()
    assert client.get("/v1/review")["json"]["held_events"] == 0


def test_a_cancelled_event_is_not_still_waiting_on_anybody(client, db_path):
    uids = _hold(db_path, 2)
    conn = db.connect(db_path)
    conn.execute("UPDATE event_state SET cancelled = 1 WHERE uid = ?", (uids[0],))
    conn.commit()
    conn.close()
    assert client.get("/v1/review")["json"]["held_events"] == 1


def test_an_answer_waiting_on_a_decision_is_counted_apart_from_a_question(
    client, db_path
):
    """Answering is work; deciding on an answer somebody gave is a glance."""
    conn = db.connect(db_path)
    repo.record_task(
        conn, task_id="t1", source_id="p360-jesse-vanguard", kind="resolve_activity",
        type="resolve_activity", context=("Skills Session",), candidates=("practice",),
        dispatched_at=NOW.isoformat())
    conn.commit()
    body = client.get("/v1/review")["json"]
    assert body["answers_awaiting_decision"] == 0, "dispatched is not yet answered"

    conn.execute("UPDATE tasks SET state = ?, answer = ?, answered_by = ? WHERE id = 't1'",
                 (repo.ANSWERED, json.dumps({"is_game": False}), "hermes/1.4"))
    conn.commit()
    conn.close()
    body = client.get("/v1/review")["json"]
    assert body["answers_awaiting_decision"] == 1
    assert body["needs_attention"] == 1


def test_an_unexplained_upstream_edit_is_something_to_go_and_look_at(client, db_path):
    conn = db.connect(db_path)
    uid = conn.execute("SELECT uid FROM event_state LIMIT 1").fetchone()["uid"]
    conn.execute("UPDATE event_state SET upstream_edit_at = ? WHERE uid = ?",
                 (NOW.isoformat(), uid))
    conn.commit()
    conn.close()
    body = client.get("/v1/review")["json"]
    assert body["upstream_edits"] == 1
    assert body["needs_attention"] == 1


def test_the_three_kinds_of_waiting_sum(client, db_path):
    _hold(db_path, 2)
    conn = db.connect(db_path)
    repo.record_task(
        conn, task_id="t1", source_id="p360-jesse-vanguard", kind="resolve_venue",
        type="resolve_venue", context=("Kingsmere",), candidates=(),
        dispatched_at=NOW.isoformat())
    conn.execute("UPDATE tasks SET state = ? WHERE id = 't1'", (repo.ANSWERED,))
    uid = conn.execute(
        "SELECT uid FROM event_state WHERE collection != 'enrichment' "
        "LIMIT 1").fetchone()["uid"]
    conn.execute("UPDATE event_state SET upstream_edit_at = ? WHERE uid = ?",
                 (NOW.isoformat(), uid))
    conn.commit()
    conn.close()

    body = client.get("/v1/review")["json"]
    assert (body["held_events"], body["answers_awaiting_decision"],
            body["upstream_edits"]) == (2, 1, 1)
    assert body["needs_attention"] == 4


def test_a_deployment_with_no_enrichment_calendar_holds_nothing(client, db_path):
    """Enrichment is switchable off, and then nothing is ever held."""
    _hold(db_path, 3)
    conn = db.connect(db_path)
    set_setting(conn, "enrichment_collection", "")
    conn.commit()
    conn.close()
    body = client.get("/v1/review")["json"]
    assert body["held_events"] == 0
    assert body["enrichment_collection"] == ""


def test_the_queue_needs_the_token_like_everything_else(client):
    assert client.get("/v1/review", token=None)["status"] == 401
