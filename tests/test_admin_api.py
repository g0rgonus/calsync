"""The admin API: a tool with an admin key creates and manages teams.

Driven through WSGI like `test_inbound.py`, whose client this reuses.
"""

from __future__ import annotations

import json
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


# --- event status --------------------------------------------------------------------

STATUS = TEAMS.replace("/teams", "/events/status")


def herons_events(client):
    push(client, "in-herons", TEAM_KEY)
    cal = client.get("/inbound/v1/calendar?from=2026-10-14&to=2026-12-31")
    assert cal["status"] == 200                       # an admin key reads the calendar
    return [e for e in cal["json"]["events"] if e["activity"]["id"] == "jesse-swim-herons"]


def set_status(client, uid, status, **extra):
    return client.call("POST", STATUS, {"uid": uid, "status": status, **extra})


def test_an_event_marked_not_attending_comes_off_and_comes_back(client, tmp_path, db_path):
    events = herons_events(client)
    uid = events[0]["uid"]
    before = len(list((tmp_path / "out").rglob("*.ics")))

    r = set_status(client, uid, "not_attending", note="School trip")
    assert r["status"] == 200
    assert r["json"] == {"uid": uid, "status": "not_attending", "on_calendar": False,
                         "detail": "off the calendar"}
    assert len(list((tmp_path / "out").rglob("*.ics"))) == before - 1

    # A later push of the same series does not bring it back.
    client.put("/inbound/v1/sources/in-herons/events/tue-swim",
               {**contract.PRACTICE, "notes": "New coach"}, key=TEAM_KEY)
    assert len(list((tmp_path / "out").rglob("*.ics"))) == before - 1

    back = set_status(client, uid, None)
    assert back["json"]["status"] is None and back["json"]["on_calendar"] is True
    assert len(list((tmp_path / "out").rglob("*.ics"))) == before

    conn = conn_for(db_path)
    log = [(e["action"], json.loads(e["detail"])) for e in reversed(repo.admin_log(conn))
           if e["action"].startswith("event_")]
    conn.close()
    assert log == [("event_withheld", {"status": "not_attending", "note": "School trip"}),
                   ("event_restored", {"status": None, "note": None})]


def test_a_cancelled_mark_uses_the_same_flag(client, db_path):
    uid = herons_events(client)[1]["uid"]
    assert set_status(client, uid, "cancelled")["json"]["on_calendar"] is False
    conn = conn_for(db_path)
    assert repo.event_state(conn, uid).withheld == "cancelled"
    conn.close()


def test_a_past_event_or_an_unknown_one_is_refused(client, clock):
    uid = herons_events(client)[0]["uid"]
    assert set_status(client, "no-such-uid", "cancelled")["status"] == 404
    clock["now"] = NOW + timedelta(days=60)
    r = set_status(client, uid, "cancelled")
    assert r["status"] == 409 and r["json"]["error"]["code"] == "already_started"


def test_a_status_change_is_checked(client):
    r = client.call("POST", STATUS, {"uid": "", "status": "sick", "note": "x" * 201,
                                     "when": "today"})
    assert r["status"] == 422
    assert {e["field"] for e in r["json"]["error"]["errors"]} == {
        "uid", "status", "note", "when"}
    missing = client.call("POST", STATUS, {"uid": "x"})
    assert missing["json"]["error"]["errors"][0]["field"] == "status"


def test_a_team_key_cannot_change_an_events_status(client):
    uid = herons_events(client)[0]["uid"]
    r = client.call("POST", STATUS, {"uid": uid, "status": "cancelled"}, key=TEAM_KEY)
    assert r["status"] == 401


def test_a_warmup_is_changed_through_its_game(client, db_path):
    conn = conn_for(db_path)
    conn.execute("UPDATE activities SET warmup_minutes = 45 WHERE id = 'jesse-swim-herons'")
    conn.commit()
    conn.close()
    push(client, "in-herons", TEAM_KEY, "sat-game",
         {"type": "game", "start": "2026-10-24T10:00:00", "end": "2026-10-24T11:00:00",
          "location": "Wrenfield Aquatic Center", "opponent": "Ridgeway Sharks"})
    cal = client.get("/inbound/v1/calendar?from=2026-10-24&to=2026-10-25")["json"]
    warm = next(e for e in cal["events"] if e["warmup_for"])
    r = set_status(client, warm["uid"], "not_attending")
    assert r["status"] == 422
    assert r["json"]["error"]["game"] == warm["warmup_for"]


@pytest.mark.parametrize("bad", [["cancelled"], {}, 3])
def test_a_status_that_is_not_text_is_a_422(client, bad):
    r = set_status(client, "x", bad)
    assert r["status"] == 422
    assert r["json"]["error"]["errors"][0]["code"] == "unknown_status"


def test_a_hidden_event_can_be_put_back_after_it_started(client, clock):
    uid = herons_events(client)[0]["uid"]
    set_status(client, uid, "cancelled")
    clock["now"] = NOW + timedelta(days=7)        # the first practice has happened
    r = set_status(client, uid, None)
    assert r["status"] == 200 and r["json"]["status"] is None


def test_putting_back_an_event_that_was_not_hidden_does_nothing(client, db_path):
    uid = herons_events(client)[0]["uid"]
    r = set_status(client, uid, None)
    assert r["json"] == {"uid": uid, "status": None, "on_calendar": True,
                         "detail": "it was not marked"}
    conn = conn_for(db_path)
    assert not [e for e in repo.admin_log(conn) if e["action"].startswith("event_")]
    conn.close()


def test_a_blank_note_is_no_note(client, db_path):
    uid = herons_events(client)[0]["uid"]
    set_status(client, uid, "cancelled", note="   ")
    conn = conn_for(db_path)
    entry = next(e for e in repo.admin_log(conn) if e["action"] == "event_withheld")
    conn.close()
    assert json.loads(entry["detail"])["note"] is None


def test_the_calendar_refusal_names_both_keys_that_open_it(client):
    detail = client.get("/inbound/v1/calendar", key=None)["json"]["error"]["detail"]
    assert detail.startswith("send the household read key or an admin key")


@pytest.fixture
def pushing(db_path, tmp_path):
    """A client whose household has Pushover set up, and the pushes it sent."""
    secrets_file = tmp_path / "push-secrets.json"
    secrets_file.write_text(json.dumps({"pushover_token": "t", "pushover_user": "u"}))
    secrets_file.chmod(0o600)
    sent = []
    client = Client(inbound_app.create_app(
        db_path, secrets=SecretStore(path=secrets_file), clock=lambda: NOW,
        log=lambda line: None, write_target=build("ics_file", directory=tmp_path / "out"),
        notify_sender=lambda config, secrets, message, **kw: sent.append(message)),
        key=ADMIN_KEY)
    return client, sent


def test_hiding_and_putting_back_an_event_each_send_a_push(pushing):
    client, sent = pushing
    uid = herons_events(client)[0]["uid"]
    set_status(client, uid, "not_attending", note="School trip")
    set_status(client, uid, None)
    assert sent == ["Grok Bot marked Herons on Tue 20 Oct 17:45 not attending: School trip.",
                    "Grok Bot put back Herons on Tue 20 Oct 17:45."]


# --- venues ---------------------------------------------------------------------------

VENUES = TEAMS.replace("/teams", "/venues")


def wrenfield(client):
    return client.get(VENUES + "?q=wren")["json"]["venues"][0]


def test_an_unknown_place_is_answered_with_an_alias_and_the_team_goes_live(client):
    source, key = staged_team(client)
    push(client, source, key, body={**contract.PRACTICE, "location": "Main pool"})
    assert client.get(f"{TEAMS}/{source}/review")["json"]["promotable"] is False

    venue = wrenfield(client)
    r = client.call("POST", f"{VENUES}/{venue['id']}/aliases", {"alias": "Main pool"})
    assert r["status"] == 200 and "Main pool" in r["json"]["aliases"]
    again = client.call("POST", f"{VENUES}/{venue['id']}/aliases", {"alias": "main POOL"})
    assert again["status"] == 200                   # the same alias, harmless

    assert client.get(f"{TEAMS}/{source}/review")["json"]["promotable"] is True
    assert client.call("POST", f"{TEAMS}/{source}/live")["status"] == 200


def test_a_venue_is_created_with_its_aliases(client, db_path):
    r = client.call("POST", VENUES, contract.NEW_VENUE)
    assert r["status"] == 201
    assert r["json"]["aliases"] == ["LM Pool", "Larkmoor", "Larkmoor Pool"]
    found = client.get(VENUES + "?q=lm pool")["json"]
    assert [v["name"] for v in found["venues"]] == ["Larkmoor Pool"]
    conn = conn_for(db_path)
    assert ("Grok Bot", "venue_created") in [(e["actor"], e["action"])
                                             for e in repo.admin_log(conn)]
    conn.close()


def test_one_place_stays_one_venue(client):
    dup = client.call("POST", VENUES, {"name": "wrenfield aquatic center"})
    assert dup["status"] == 409 and dup["json"]["error"]["code"] == "already_exists"
    assert dup["json"]["error"]["venue"]["name"] == "Wrenfield Aquatic Center"

    client.call("POST", VENUES, contract.NEW_VENUE)
    taken = client.call("POST", VENUES, {"name": "Fenwick Natatorium", "aliases": ["Larkmoor"]})
    assert taken["status"] == 409 and taken["json"]["error"]["code"] == "alias_taken"
    wren = wrenfield(client)
    steal = client.call("POST", f"{VENUES}/{wren['id']}/aliases", {"alias": "LM Pool"})
    assert steal["status"] == 409
    assert steal["json"]["error"]["venue"]["name"] == "Larkmoor Pool"


def test_a_field_number_is_not_a_venue(client):
    r = client.call("POST", VENUES, {"name": "Kingsmere #2"})
    assert r["status"] == 422
    error = r["json"]["error"]["errors"][0]
    assert error["code"] == "field_in_name" and error["suggestion"] == "Kingsmere"


def wsgi(path: str) -> str:
    """A path as a WSGI server hands it over: UTF-8 bytes, read as Latin-1."""
    return path.encode().decode("latin-1")


@pytest.mark.parametrize("path", ["/999/aliases", "/abc/aliases", wsgi("/²/aliases"),
                                  wsgi("/١/aliases"), "/" + "9" * 30 + "/aliases"])
def test_an_alias_for_no_venue_is_404(client, path):
    assert client.call("POST", VENUES + path, {"alias": "x"})["status"] == 404


def test_a_teams_home_venue_is_set_and_cleared(client):
    wren = wrenfield(client)
    r = client.call("PUT", f"{TEAMS}/in-herons/home-venue", {"venue_id": wren["id"]})
    assert r["json"] == {"source_id": "in-herons", "home_venue_id": wren["id"],
                         "home_venue": "Wrenfield Aquatic Center"}
    assert wrenfield(client)["home_to"] == ["Herons"]
    cleared = client.call("PUT", f"{TEAMS}/in-herons/home-venue", {"venue_id": None})
    assert cleared["json"]["home_venue"] is None
    assert client.call("PUT", f"{TEAMS}/in-herons/home-venue",
                       {"venue_id": 999})["status"] == 404
    assert client.call("PUT", f"{TEAMS}/in-herons/home-venue",
                       {"venue": 1})["status"] == 422


def test_venues_need_an_admin_key(client):
    assert client.get(VENUES, key=TEAM_KEY)["status"] == 401
    assert client.call("POST", VENUES, contract.NEW_VENUE, key=TEAM_KEY)["status"] == 401


@pytest.mark.parametrize("bad", [0, -1, 10 ** 30, "7", True])
def test_a_home_venue_id_must_be_a_real_id(client, bad):
    r = client.call("PUT", f"{TEAMS}/in-herons/home-venue", {"venue_id": bad})
    assert r["status"] == 422


def test_aliases_that_differ_only_in_case_are_one(client):
    r = client.call("POST", VENUES, {"name": "Larkmoor Pool",
                                     "aliases": ["Larkmoor", " larkmoor ", "LARKMOOR POOL"]})
    assert r["json"]["aliases"] == ["Larkmoor", "Larkmoor Pool"]


def test_an_alias_added_by_mistake_is_removed(client):
    wren = wrenfield(client)
    aliases = f"{VENUES}/{wren['id']}/aliases"
    client.call("POST", aliases, {"alias": "Main pool"})
    r = client.call("DELETE", aliases, {"alias": "main pool"})
    assert r["status"] == 200 and "Main pool" not in r["json"]["aliases"]
    assert client.call("DELETE", aliases, {"alias": "Main pool"})["status"] == 200
    own = client.call("DELETE", aliases, {"alias": "Wrenfield Aquatic Center"})
    assert own["status"] == 409 and own["json"]["error"]["code"] == "own_name"


def test_every_admin_change_that_can_touch_a_live_calendar_sends_a_push(pushing):
    client, sent = pushing
    source, key = staged_team(client)                       # staged: silent
    client.call("POST", f"{TEAMS}/{source}/pause")          # staged: silent
    client.call("POST", f"{TEAMS}/{source}/resume")
    client.call("POST", f"{TEAMS}/in-herons/key")           # keys: silent
    assert sent == []

    client.call("POST", f"{TEAMS}/in-herons/pause")
    client.call("POST", f"{TEAMS}/in-herons/pause")         # already paused: no change
    client.call("POST", f"{TEAMS}/in-herons/resume")
    venue = client.call("POST", VENUES, {"name": "Larkmoor Pool"})["json"]
    client.call("POST", f"{VENUES}/{venue['id']}/aliases", {"alias": "Main pool"})
    client.call("DELETE", f"{VENUES}/{venue['id']}/aliases", {"alias": "Main pool"})
    client.call("PUT", f"{TEAMS}/in-herons/home-venue", {"venue_id": venue["id"]})
    client.call("PUT", f"{TEAMS}/in-herons/home-venue", {"venue_id": None})
    push(client, source, key)
    client.call("POST", f"{TEAMS}/{source}/live")
    assert sent == [
        "Grok Bot paused Herons.",
        "Grok Bot resumed Herons.",
        "Grok Bot added the venue Larkmoor Pool.",
        'Grok Bot: "Main pool" now means Larkmoor Pool.',
        'Grok Bot: "Main pool" no longer means Larkmoor Pool.',
        "Grok Bot set Herons's home venue to Larkmoor Pool.",
        "Grok Bot cleared Herons's home venue.",
        "Grok Bot made Kestrels live.",
    ]
