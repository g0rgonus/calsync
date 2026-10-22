"""Kids on one team: each event written once, titled for every kid going.

Driven through the inbound API and the console against one calendar on disk,
so a title is an assertion about what a phone would show.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
from icalendar import Calendar

from calsync import db, repo
from calsync.inbound import app as inbound_app
from calsync.inbound import contract
from calsync.inbound import events as inbound
from calsync.secrets import SecretStore
from calsync.settings import Settings, set_setting
from calsync.targets import TargetError, build
from calsync.web import app as web_app

from test_inbound import Client as ApiClient
from test_web import Client as ConsoleClient

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ADMIN_KEY = "csk_test-admin-key-for-a-tool-000000000000"
TEAMS = "/inbound/v1/admin/teams"
STATUS = "/inbound/v1/admin/events/status"
PRACTICE = {**contract.PRACTICE, "location": "Wrenfield Aquatic Center"}


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "calsync.db"
    conn = db.open_db(path)
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('jesse', 'Jesse', 'J', 1), ('nadia', 'Nadia', 'N', 2);
        """
    )
    repo.issue_admin_key(conn, "Grok Bot", inbound.hash_key(ADMIN_KEY))
    repo.upsert_venue(conn, name="Wrenfield Aquatic Center")
    conn.close()
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"pushover_token": "t", "pushover_user": "u"}))
    secrets_file.chmod(0o600)
    secrets = SecretStore(path=secrets_file, environ={})
    target = build("ics_file", directory=tmp_path / "out")
    sent = []
    api = ApiClient(inbound_app.create_app(
        path, secrets=secrets, clock=lambda: NOW, log=lambda line: None,
        write_target=target, notify_sender=lambda c, s, message, **kw: sent.append(message)),
        key=ADMIN_KEY)
    console = ConsoleClient(web_app.create_app(path, secrets=secrets, clock=lambda: NOW,
                                               write_target=target))
    return {"path": path, "api": api, "console": console, "out": tmp_path / "out",
            "sent": sent, "secrets": secrets, "target": target}


def team(env, name, children, idem=None):
    r = env["api"].call("POST", TEAMS, {"name": name, "children": children,
                                        "sport": "swimming", "tz": "America/New_York"},
                        headers={"Idempotency-Key": idem or f"{name}-{'-'.join(children)}"})
    assert r["status"] == 201, r["json"]
    return r["json"]["source_id"], r["json"]["team_key"]


def live(env, source, key):
    assert env["api"].put(f"/inbound/v1/sources/{source}/events/tue-swim", PRACTICE,
                          key=key)["status"] == 201
    assert env["api"].call("POST", f"{TEAMS}/{source}/live")["status"] == 200


def titles(env) -> dict[str, list[str]]:
    """Summaries on the calendar, by date."""
    out: dict[str, list[str]] = {}
    for f in env["out"].rglob("*.ics"):
        ev = next(c for c in Calendar.from_ical(f.read_bytes()).walk() if c.name == "VEVENT")
        out.setdefault(ev.decoded("DTSTART").date().isoformat(), []).append(str(ev["SUMMARY"]))
    return out


def uid_on(env, date: str) -> str:
    cal = env["api"].get(f"/inbound/v1/calendar?from={date}&to={date}T23:59:59Z")["json"]
    return cal["events"][0]["uid"]


def test_a_shared_team_writes_each_event_once_for_both_kids(env):
    source, key = team(env, "Herons", ["nadia", "jesse"])
    live(env, source, key)
    by_day = titles(env)
    assert by_day["2026-10-20"] == ["J+N 🏊 Swim"]
    assert all(len(t) == 1 for t in by_day.values())
    cal = env["api"].get("/inbound/v1/calendar?from=2026-10-20&to=2026-10-21")["json"]
    assert [c["id"] for c in cal["events"][0]["children"]] == ["jesse", "nadia"]
    assert env["api"].get("/inbound/v1/admin/teams")["json"]["teams"][0]["children"] == [
        "jesse", "nadia"]


def test_one_kid_missing_one_event_drops_from_its_title_only(env):
    source, key = team(env, "Herons", ["jesse", "nadia"])
    live(env, source, key)
    uid = uid_on(env, "2026-10-20")
    r = env["api"].call("POST", STATUS, {"uid": uid, "status": "not_attending",
                                         "child": "nadia", "note": "Dentist"})
    assert r["status"] == 200
    assert r["json"]["going"] == ["jesse"] and r["json"]["on_calendar"] is True
    by_day = titles(env)
    assert by_day["2026-10-20"] == ["Jesse 🏊 Swim"]
    assert by_day["2026-10-27"] == ["J+N 🏊 Swim"]
    assert env["sent"][-1] == ("Grok Bot marked Nadia not attending Herons on "
                               "Tue 20 Oct 17:45: Dentist.")
    # Busy for the kid going, not for the one who is not.
    nadia = env["api"].get("/inbound/v1/calendar?from=2026-10-20&to=2026-10-21&child=nadia")
    assert nadia["json"]["events"] == []


def test_the_event_comes_off_when_nobody_is_left_and_back_with_one_kid(env):
    source, key = team(env, "Herons", ["jesse", "nadia"])
    live(env, source, key)
    uid = uid_on(env, "2026-10-20")
    for kid in ("jesse", "nadia"):
        r = env["api"].call("POST", STATUS, {"uid": uid, "status": "not_attending",
                                             "child": kid})
    assert r["json"]["on_calendar"] is False
    assert "2026-10-20" not in titles(env)
    back = env["api"].call("POST", STATUS, {"uid": uid, "status": None, "child": "nadia"})
    assert back["json"]["going"] == ["nadia"] and back["json"]["on_calendar"] is True
    assert titles(env)["2026-10-20"] == ["Nadia 🏊 Swim"]


def test_a_kid_not_on_the_team_or_a_cancelled_child_is_refused(env):
    source, key = team(env, "Herons", ["jesse"])
    live(env, source, key)
    uid = uid_on(env, "2026-10-20")
    r = env["api"].call("POST", STATUS, {"uid": uid, "status": "not_attending",
                                         "child": "nadia"})
    assert r["status"] == 422 and r["json"]["error"]["errors"][0]["code"] == "not_on_team"
    r = env["api"].call("POST", STATUS, {"uid": uid, "status": "cancelled", "child": "jesse"})
    assert r["status"] == 422


def test_merging_adds_the_kid_and_retires_their_own_copy(env):
    herons, herons_key = team(env, "Herons", ["jesse"])
    live(env, herons, herons_key)
    copy, copy_key = team(env, "Herons", ["nadia"])
    live(env, copy, copy_key)
    assert sorted(titles(env)["2026-10-20"]) == ["Jesse 🏊 Swim", "Nadia 🏊 Swim"]

    r = env["console"].post(f"/sources/{herons}/kids", {"child": "nadia", "retire": copy})
    assert r["status"] == 303 and "retired" in r["headers"]["Location"]
    assert titles(env)["2026-10-20"] == ["J+N 🏊 Swim"]
    gone = env["api"].get(f"/inbound/v1/sources/{copy}", key=copy_key)
    assert gone["status"] == 401 and gone["json"]["error"]["code"] == "revoked"


def test_only_the_added_kids_own_copy_can_be_retired(env):
    herons, herons_key = team(env, "Herons", ["jesse"])
    live(env, herons, herons_key)
    copy, copy_key = team(env, "Herons", ["nadia"])
    live(env, copy, copy_key)
    otters, otters_key = team(env, "Otters", ["jesse"])
    live(env, otters, otters_key)
    page = env["console"].get(f"/sources/{herons}")["body"]
    assert f"and retire Nadia's Herons ({copy})" in page and otters not in page

    r = env["console"].post(f"/sources/{herons}/kids", {"child": "nadia", "retire": otters})
    assert r["status"] >= 400
    assert env["api"].get(f"/inbound/v1/sources/{otters}", key=otters_key)["status"] == 200
    assert len(titles(env)["2026-10-20"]) == 3


def test_a_partial_retire_keeps_the_other_teams_key(env):
    class Refusing:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def cancel(self, ref):
            raise TargetError("calendar said no")

    herons, herons_key = team(env, "Herons", ["jesse"])
    live(env, herons, herons_key)
    copy, copy_key = team(env, "Herons", ["nadia"])
    live(env, copy, copy_key)
    console = ConsoleClient(web_app.create_app(
        env["path"], secrets=env["secrets"], clock=lambda: NOW,
        write_target=Refusing(env["target"])))
    r = console.post(f"/sources/{herons}/kids", {"child": "nadia", "retire": copy})
    assert r["status"] == 303 and "err=" in r["headers"]["Location"]
    assert "keeps%20its%20key" in r["headers"]["Location"]
    assert env["api"].get(f"/inbound/v1/sources/{copy}", key=copy_key)["status"] == 200


def test_splitting_a_pushed_team_gives_the_kid_their_events_back(env):
    source, key = team(env, "Herons", ["jesse", "nadia"])
    live(env, source, key)
    page = env["console"].post(f"/sources/{source}/kids/split", {"child": "nadia"})
    assert page["status"] == 200
    new_key = re.search(r"csk_[A-Za-z0-9_-]{20,}", page["body"]).group(0)
    assert sorted(titles(env)["2026-10-20"]) == ["Jesse 🏊 Swim",
                                                 "Nadia 🏊 Swim"]
    teams = env["api"].get(TEAMS)["json"]["teams"]
    nadias = next(t for t in teams if t["children"] == ["nadia"])
    assert env["api"].get(f"/inbound/v1/sources/{nadias['source_id']}",
                          key=new_key)["status"] == 200


def test_splitting_keeps_the_kids_absences_off_their_new_team(env):
    source, key = team(env, "Herons", ["jesse", "nadia"])
    live(env, source, key)
    uid = uid_on(env, "2026-10-20")
    env["api"].call("POST", STATUS, {"uid": uid, "status": "not_attending", "child": "nadia"})
    assert env["console"].post(f"/sources/{source}/kids/split",
                               {"child": "nadia"})["status"] == 200
    by_day = titles(env)
    assert by_day["2026-10-20"] == ["Jesse 🏊 Swim"]
    assert sorted(by_day["2026-10-27"]) == ["Jesse 🏊 Swim", "Nadia 🏊 Swim"]


def test_splitting_a_staged_team_keeps_the_copy_staged(env):
    source, key = team(env, "Herons", ["jesse", "nadia"])
    assert env["api"].put(f"/inbound/v1/sources/{source}/events/tue-swim", PRACTICE,
                          key=key)["status"] == 201
    assert env["console"].post(f"/sources/{source}/kids/split",
                               {"child": "nadia"})["status"] == 200
    conn = db.connect(env["path"])
    copy = next(s for s in repo.list_sources(conn) if s.id != source)
    assert copy.staging_collection
    conn.close()


def test_calendars_cannot_be_split_by_kid_while_a_team_is_shared(env):
    team(env, "Herons", ["jesse", "nadia"])
    r = env["console"].post("/settings/calendar", {"collection_template": "{child}-{type}"})
    assert r["status"] >= 400
    conn = db.connect(env["path"])
    assert "{child}" not in Settings.load(conn).collection_template
    conn.close()


def test_a_team_cannot_be_shared_when_calendars_are_split_by_kid(env):
    conn = db.connect(env["path"])
    set_setting(conn, "collection_template", "{child}-{type}")
    conn.close()
    r = env["api"].call("POST", TEAMS, {"name": "Herons", "children": ["jesse", "nadia"],
                                        "sport": "swimming"},
                        headers={"Idempotency-Key": "x"})
    assert r["status"] == 422 and r["json"]["error"]["code"] == "per_kid_calendars"
    source, _ = team(env, "Herons", ["jesse"])
    refused = env["console"].post(f"/sources/{source}/kids", {"child": "nadia"})
    assert refused["status"] >= 400


def test_a_shared_teams_name_is_taken_for_each_of_its_kids(env):
    team(env, "Herons", ["jesse", "nadia"])
    r = env["api"].call("POST", TEAMS, {"name": "herons", "children": ["nadia"],
                                        "sport": "swimming"},
                        headers={"Idempotency-Key": "again"})
    assert r["status"] == 409


def test_the_console_marks_one_kid_out_of_one_event(env):
    source, key = team(env, "Herons", ["jesse", "nadia"])
    live(env, source, key)
    uid = uid_on(env, "2026-10-20")
    page = env["console"].get("/calendar?view=agenda&month=2026-10")["body"]
    assert "Nadia not attending" in page

    out = env["console"].post("/events/withhold", {"uid": uid, "reason": "not_attending",
                                                   "child": "nadia"})
    assert out["status"] == 303
    assert titles(env)["2026-10-20"] == ["Jesse 🏊 Swim"]
    assert "Nadia not going" in env["console"].get("/calendar?view=agenda&month=2026-10")["body"]
    refused = env["console"].post("/events/withhold", {"uid": uid, "reason": "cancelled",
                                                       "child": "jesse"})
    assert refused["status"] >= 400

    back = env["console"].post("/events/restore", {"uid": uid, "child": "nadia"})
    assert back["status"] == 303
    assert titles(env)["2026-10-20"] == ["J+N 🏊 Swim"]


def test_splitting_a_feed_team_keeps_the_feed_on_the_team(env):
    conn = db.connect(env["path"])
    conn.executescript(
        """
        INSERT INTO activities (id, child_id, name, sport_id, tz, alarm_game_min,
                                alarm_practice_min)
             VALUES ('jesse-soccer-wrens', 'jesse', 'Wrens', 'soccer', 'UTC', 90, 30);
        INSERT INTO activity_children VALUES ('jesse-soccer-wrens', 'nadia');
        INSERT INTO sources (id, activity_id, kind, shape, enabled)
             VALUES ('tr-wrens', 'jesse-soccer-wrens', 'teamreach', 'feed', 0);
        """
    )
    conn.commit()
    conn.close()
    r = env["console"].post("/sources/tr-wrens/kids/split", {"child": "nadia"})
    assert r["status"] == 303 and "feed" in r["headers"]["Location"]
    teams = {t["source_id"]: t["children"] for t in env["api"].get(TEAMS)["json"]["teams"]}
    assert teams == {"tr-wrens": ["jesse"]}
