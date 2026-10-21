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
from calsync.targets import build

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
    repo.upsert_venue(conn, name="Wrenfield Aquatic Center",
                      address="1 Pool Rd, Marbury NX 40114")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def clock():
    return {"now": NOW}


@pytest.fixture
def client(db_path, tmp_path, clock):
    app = inbound_app.create_app(
        db_path, secrets=SecretStore(path=tmp_path / "secrets.json"),
        clock=lambda: clock["now"], log=lambda line: None,
        write_target=build("ics_file", directory=tmp_path / "out"))
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


# --- staging: review, make live, discard -------------------------------------------


def staged_team(client, name="Kestrels", **extra):
    r = create(client, {**contract.NEW_TEAM, "name": name, **extra}, idem=f"create-{name}")
    return r["json"]["source_id"], r["json"]["team_key"]


def push(client, source, key, external_id="tue-swim", body=None):
    r = client.put(f"/inbound/v1/sources/{source}/events/{external_id}",
                   body or contract.PRACTICE, key=key)
    assert r["status"] == 201, r["json"]


def ics_dirs(tmp_path) -> set[str]:
    return {p.parent.name for p in (tmp_path / "out").rglob("*.ics")}


def test_an_unknown_venue_keeps_a_team_staged(client):
    source, key = staged_team(client)
    push(client, source, key, body={**contract.PRACTICE, "location": "Nowhere Hall"})
    review = client.get(f"{TEAMS}/{source}/review")["json"]
    assert review["not_ready"] == "the parse still has gaps"
    assert {c["key"]: c["state"] for c in review["checks"]}["venues"] == "asking"


def test_a_staged_team_with_nothing_sent_is_not_ready(client):
    source, _ = staged_team(client)
    review = client.get(f"{TEAMS}/{source}/review")["json"]
    assert review["promotable"] is False
    assert review["not_ready"] == "nothing has been sent yet"
    assert len(review["checks"]) == 4
    r = client.call("POST", f"{TEAMS}/{source}/live")
    assert r["status"] == 409 and r["json"]["error"]["code"] == "not_ready"


def test_the_review_shows_where_events_will_land_and_what_they_clash_with(client):
    source, key = staged_team(client, tz="America/Chicago")
    push(client, source, key)
    push(client, "in-herons", TEAM_KEY)          # the same Tuesdays, another kid
    review = client.get(f"{TEAMS}/{source}/review")["json"]
    assert review["promotable"] is True
    first = review["events"][0]
    assert first["collection"] == "onboarding"
    assert first["lands_in"] == "practices"
    assert review["clashes"]
    clash = review["clashes"][0]
    assert clash["with"]["child"]["id"] == "jesse"
    assert clash["with"]["activity"]["id"] == "jesse-swim-herons"


def test_making_a_team_live_moves_its_events_now(client, tmp_path, db_path):
    source, key = staged_team(client)
    push(client, source, key)
    assert ics_dirs(tmp_path) == {"onboarding"}
    r = client.call("POST", f"{TEAMS}/{source}/live")
    assert r["status"] == 200
    assert r["json"]["staged_to"] is None
    assert r["json"]["sync"]["status"] == "ok"
    assert ics_dirs(tmp_path) == {"practices"}
    conn = conn_for(db_path)
    assert ("Grok Bot", "team_made_live") in [
        (e["actor"], e["action"]) for e in repo.admin_log(conn)]
    conn.close()


def test_the_api_cannot_force_a_team_live(client):
    source, _ = staged_team(client)
    r = client.call("POST", f"{TEAMS}/{source}/live?force=true")
    assert r["status"] == 409 and r["json"]["error"]["code"] == "not_ready"


def test_making_a_live_team_live_changes_nothing(client):
    r = client.call("POST", f"{TEAMS}/in-herons/live")
    assert r["status"] == 200 and "sync" not in r["json"]


def test_discarding_takes_past_staged_events_too(client, tmp_path, clock):
    source, key = staged_team(client)
    push(client, source, key)
    clock["now"] = NOW + timedelta(days=120)       # every practice has happened
    assert client.delete(f"{TEAMS}/{source}")["status"] == 204
    assert ics_dirs(tmp_path) == set()


def test_a_retry_of_a_discarded_teams_create_is_gone(client):
    source, _ = staged_team(client)
    client.delete(f"{TEAMS}/{source}")
    r = create(client, contract.NEW_TEAM, idem="create-Kestrels")
    assert r["status"] == 410 and r["json"]["error"]["code"] == "team_discarded"


def test_a_discarded_team_leaves_nothing_behind(client, tmp_path, db_path):
    source, key = staged_team(client)
    push(client, source, key)
    assert client.delete(f"{TEAMS}/{source}")["status"] == 204
    assert ics_dirs(tmp_path) == set()
    assert source not in [t["source_id"] for t in client.get(TEAMS)["json"]["teams"]]
    assert client.get(f"/inbound/v1/sources/{source}", key=key)["status"] == 401
    conn = conn_for(db_path)
    assert conn.execute("SELECT 1 FROM activities WHERE name = 'Kestrels'").fetchone() is None
    conn.close()
    # The name is free again.
    assert create(client, contract.NEW_TEAM, idem="again")["status"] == 201


def test_a_live_team_cannot_be_discarded(client):
    r = client.delete(f"{TEAMS}/in-herons")
    assert r["status"] == 409 and r["json"]["error"]["code"] == "not_staged"


@pytest.mark.parametrize("method, path", [
    ("GET", "/nope/review"), ("POST", "/nope/live"), ("DELETE", "/nope")])
def test_staging_calls_on_an_unknown_team_are_404(client, method, path):
    assert client.call(method, TEAMS + path)["status"] == 404


def test_the_same_wall_clock_in_another_zone_is_no_clash(client):
    source, key = staged_team(client, tz="Asia/Tokyo")
    push(client, source, key)
    push(client, "in-herons", TEAM_KEY)          # 17:45 in Chicago, hours apart
    assert client.get(f"{TEAMS}/{source}/review")["json"]["clashes"] == []


def test_a_discard_the_calendar_half_refuses_says_what_happened(db_path, tmp_path):
    from calsync.targets import TargetError

    class Refusing:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def cancel(self, ref):
            raise TargetError("calendar said no")

    inner = build("ics_file", directory=tmp_path / "out")
    writing = Client(inbound_app.create_app(
        db_path, secrets=SecretStore(path=tmp_path / "secrets.json"),
        clock=lambda: NOW, log=lambda line: None, write_target=inner), key=ADMIN_KEY)
    source, key = staged_team(writing)
    push(writing, source, key)
    refusing = Client(inbound_app.create_app(
        db_path, secrets=SecretStore(path=tmp_path / "secrets.json"),
        clock=lambda: NOW, log=lambda line: None, write_target=Refusing(inner)),
        key=ADMIN_KEY)
    r = refusing.delete(f"{TEAMS}/{source}")
    assert r["status"] == 503
    import re as _re
    assert _re.match(r"0 staged events removed, [1-9]\d* could not be; the team was not "
                     r"discarded", r["json"]["error"]["detail"])
    assert source in [t["source_id"] for t in refusing.get(TEAMS)["json"]["teams"]]
