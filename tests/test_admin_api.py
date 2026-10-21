"""The admin API: a tool with an admin key creates and manages teams.

Driven through WSGI like `test_inbound.py`, whose client this reuses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from calsync import db, repo
from calsync.inbound import app as inbound_app
from calsync.inbound import contract
from calsync.inbound import events as inbound
from calsync.secrets import SecretStore

from test_inbound import Client

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ADMIN_KEY = "csk_test-admin-key-for-a-tool-000000000000"
TEAM_KEY = "csk_test-key-for-the-herons-team-00000000"
TEAMS = "/inbound/v1/admin/teams"


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
                     'America/Chicago', 60, 20);
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('in-herons', 'jesse-swim-herons', 'inbound', 'push');
        """
    )
    repo.set_inbound_key_hash(conn, "in-herons", inbound.hash_key(TEAM_KEY))
    repo.issue_admin_key(conn, "Grok Bot", inbound.hash_key(ADMIN_KEY))
    conn.close()
    return path


@pytest.fixture
def clock():
    return {"now": NOW}


@pytest.fixture
def client(db_path, tmp_path, clock):
    app = inbound_app.create_app(
        db_path, secrets=SecretStore(path=tmp_path / "secrets.json"),
        clock=lambda: clock["now"], log=lambda line: None)
    return Client(app, key=ADMIN_KEY)


def create(client, body, idem="create-1", **kw):
    headers = {"Idempotency-Key": idem} if idem else {}
    return client.call("POST", TEAMS, body, headers=headers, **kw)


def conn_for(db_path):
    return db.connect(db_path)


def test_a_team_is_created_staged_with_its_key_shown_once(client, db_path):
    r = create(client, contract.NEW_TEAM)
    assert r["status"] == 201
    team = r["json"]
    assert team["name"] == "Kestrels" and team["children"] == ["nadia"]
    assert team["sport"] == "robotics" and team["kind"] == "inbound"
    assert team["staged_to"] == "onboarding"
    assert team["key"]["id"]

    # The key it was given opens the new team, and only that team.
    key = team["team_key"]
    source = team["source_id"]
    assert client.get(f"/inbound/v1/sources/{source}", key=key)["status"] == 200
    assert client.get("/inbound/v1/sources/in-herons", key=key)["status"] == 401
    assert key.encode() not in db_path.read_bytes()

    conn = conn_for(db_path)
    log = [(r["actor"], r["action"]) for r in reversed(repo.admin_log(conn))]
    conn.close()
    assert ("Grok Bot", "team_created") in log
    assert ("Grok Bot", "key_issued") in log


def test_a_retry_returns_the_team_without_the_key_and_makes_nothing_twice(client):
    first = create(client, contract.NEW_TEAM)
    again = create(client, contract.NEW_TEAM)
    assert again["status"] == 201
    assert again["headers"]["idempotent-replayed"] == "true"
    assert again["json"]["key_already_issued"] is True
    assert "team_key" not in again["json"]
    assert again["json"]["source_id"] == first["json"]["source_id"]
    assert client.get(TEAMS)["json"]["count"] == 2


def test_an_idempotency_key_reused_with_another_body_is_refused(client):
    create(client, contract.NEW_TEAM)
    r = create(client, {**contract.NEW_TEAM, "name": "Falcons"})
    assert r["status"] == 422
    assert r["json"]["error"]["code"] == "idempotency_key_reused"


def test_a_replay_expires_after_a_day(client, clock):
    create(client, contract.NEW_TEAM)
    clock["now"] = NOW + timedelta(hours=25)
    # The record is gone, so the natural duplicate check answers instead.
    r = create(client, contract.NEW_TEAM)
    assert r["json"]["error"]["code"] == "already_exists"


def test_a_create_needs_an_idempotency_key(client):
    r = create(client, contract.NEW_TEAM, idem=None)
    assert r["status"] == 400
    assert r["json"]["error"]["code"] == "idempotency_key_required"


def test_the_same_team_for_the_same_kid_is_already_there(client):
    r = create(client, {"name": "herons ", "children": ["jesse"], "sport": "swimming"})
    assert r["status"] == 409
    assert r["json"]["error"]["code"] == "already_exists"
    assert r["json"]["error"]["team"]["source_id"] == "in-herons"
    # The same name for another kid is a different team.
    other = create(client, {"name": "Herons", "children": ["nadia"], "sport": "swimming"},
                   idem="create-2")
    assert other["status"] == 201


def test_every_problem_is_listed_with_the_choices(client):
    r = create(client, {"name": "", "children": ["zoe"], "sport": "quidditch",
                        "tz": "EDT", "season_end": "May", "alarm_game_min": -5,
                        "calendar": "games"})
    assert r["status"] == 422
    errors = {e["field"]: e for e in r["json"]["error"]["errors"]}
    assert set(errors) == {"name", "children", "sport", "tz", "season_end",
                           "alarm_game_min", "calendar"}
    assert errors["children"]["candidates"] == ["jesse", "nadia"]
    assert "swimming" in errors["sport"]["candidates"]
    assert errors["calendar"]["code"] == "unknown_field"


def test_one_kid_per_team_for_now(client):
    r = create(client, {**contract.NEW_TEAM, "children": ["nadia", "jesse"]})
    assert r["json"]["error"]["errors"][0]["code"] == "one_child_only"


def test_a_new_team_takes_the_kids_last_season_in_that_sport(client):
    r = create(client, {"name": "Otters", "children": ["jesse"], "sport": "swimming"})
    assert r["json"]["tz"] == "America/Chicago"


def test_a_team_can_be_paused_and_resumed(client):
    paused = client.call("POST", TEAMS + "/in-herons/pause")
    assert paused["json"]["enabled"] is False
    write = client.put("/inbound/v1/sources/in-herons/events/x", contract.PRACTICE,
                       key=TEAM_KEY)
    assert write["json"]["error"]["code"] == "source_disabled"
    assert client.call("POST", TEAMS + "/in-herons/resume")["json"]["enabled"] is True
    assert client.call("POST", TEAMS + "/nope/pause")["status"] == 404


def test_a_lost_team_key_is_replaced_and_the_old_one_stops(client):
    r = client.call("POST", TEAMS + "/in-herons/key")
    assert r["status"] == 201
    fresh = r["json"]["team_key"]
    assert client.get("/inbound/v1/sources/in-herons", key=TEAM_KEY)["status"] == 401
    assert client.get("/inbound/v1/sources/in-herons", key=fresh)["status"] == 200

    assert client.delete(TEAMS + "/in-herons/key")["status"] == 204
    assert client.get("/inbound/v1/sources/in-herons", key=fresh)["status"] == 401
    assert client.delete(TEAMS + "/in-herons/key")["status"] == 204   # idempotent
    listed = client.get(TEAMS)["json"]["teams"]
    assert [t["key"] for t in listed if t["source_id"] == "in-herons"] == [None]


@pytest.mark.parametrize("key", [None, TEAM_KEY, "csk_wrong"])
def test_only_an_admin_key_opens_the_admin_api(client, key):
    assert client.get(TEAMS, key=key)["status"] == 401
    assert create(client, contract.NEW_TEAM, key=key)["status"] == 401


def test_a_revoked_admin_key_stops_at_once(client, db_path):
    conn = conn_for(db_path)
    repo.revoke_key(conn, repo.admin_key(conn, inbound.hash_key(ADMIN_KEY)).id)
    conn.close()
    assert client.get(TEAMS)["status"] == 401


@pytest.mark.parametrize("bad, field", [
    ({"children": [["nadia"]]}, "children"),
    ({"children": [{"id": "nadia"}]}, "children"),
    ({"sport": ["swimming"]}, "sport"),
    ({"season_end": "20270531"}, "season_end"),
    ({"season_end": 20270531}, "season_end"),
])
def test_a_wrongly_typed_field_is_a_422_not_a_crash(client, bad, field):
    r = create(client, {**contract.NEW_TEAM, **bad})
    assert r["status"] == 422
    assert [e["field"] for e in r["json"]["error"]["errors"]] == [field]


def test_a_null_field_takes_its_default(client):
    r = create(client, {**contract.NEW_TEAM, "alarm_game_min": None, "tz": None})
    assert r["status"] == 201


def test_the_admin_refusal_asks_for_an_admin_key(client):
    detail = client.get(TEAMS, key=None)["json"]["error"]["detail"]
    assert detail.startswith("send an admin key")
