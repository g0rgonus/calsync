"""Kids on different teams at one meet: one event, titled and described for both.

Driven through the inbound API against one calendar on disk, like
test_shared_teams.py, so a title is an assertion about what a phone would show.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote
from datetime import datetime, timedelta, timezone

import pytest
from icalendar import Calendar

from calsync import correlate, db, repo
from calsync.settings import set_setting
from calsync.inbound import app as inbound_app
from calsync.inbound import events as inbound
from calsync.secrets import SecretStore
from calsync import merging
from calsync import sync as sync_module
from calsync.sync import NoTarget, sync_source
from calsync.targets import TargetError, build

from calsync.api import app as api_app
from calsync.web import app as web_app

from test_api import Client as ReadClient
from test_inbound import Client as ApiClient
from test_web import Client as ConsoleClient

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ADMIN_KEY = "csk_test-admin-key-for-a-tool-000000000000"
TEAMS = "/inbound/v1/admin/teams"
STATUS = "/inbound/v1/admin/events/status"
MEET = {"type": "game", "label": "Harbor Invitational", "start": "2026-11-14",
        "end": "2026-11-15", "location": "Wrenfield Aquatic Center"}
DAY = "2026-11-14"
HERONS_UID = "in-herons.harbor@inbound.calsync"
OTTERS_UID = "in-otters.harbor@inbound.calsync"
MERGED = f"merged.{HERONS_UID}"


class Recording:
    """The calendar, remembering writes in order and refusing the ones it is told to."""

    def __init__(self, inner):
        self.inner, self.calls, self.fail = inner, [], ()

    def upsert(self, event, previous=None):
        if event.uid.startswith(self.fail):
            raise TargetError(f"refused {event.uid}")
        self.calls.append(("upsert", event.uid))
        return self.inner.upsert(event, previous)

    def cancel(self, ref):
        if ref.remote_id.startswith(self.fail):
            raise TargetError(f"refused {ref.remote_id}")
        self.calls.append(("cancel", ref.remote_id))
        return self.inner.cancel(ref)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def at(self, kind, uid):
        return self.calls.index((kind, uid))


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "calsync.db"
    conn = db.open_db(path)
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('jesse', 'Jesse', 'J', 1), ('nadia', 'Nadia', 'N', 2),
                    ('sam', 'Sam', 'S', 3);
        """
    )
    repo.issue_admin_key(conn, "Grok Bot", inbound.hash_key(ADMIN_KEY))
    repo.upsert_venue(conn, name="Wrenfield Aquatic Center")
    repo.upsert_venue(conn, name="Ridgeway Natatorium")
    conn.close()
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"pushover_token": "t", "pushover_user": "u"}))
    secrets_file.chmod(0o600)
    secrets = SecretStore(path=secrets_file, environ={})
    target = Recording(build("ics_file", directory=tmp_path / "out"))
    api = ApiClient(inbound_app.create_app(
        path, secrets=secrets, clock=lambda: NOW, log=lambda line: None,
        write_target=target, notify_sender=lambda c, s, message, **kw: None),
        key=ADMIN_KEY)
    console = ConsoleClient(web_app.create_app(path, secrets=secrets, clock=lambda: NOW,
                                               write_target=target))
    return {"path": path, "api": api, "console": console, "out": tmp_path / "out",
            "target": target, "secrets": secrets}


def team(env, name, child):
    r = env["api"].call("POST", TEAMS, {"name": name, "children": [child],
                                        "sport": "swimming", "tz": "America/New_York"},
                        headers={"Idempotency-Key": name})
    assert r["status"] == 201, r["json"]
    return r["json"]["source_id"], r["json"]["team_key"]


def push(env, source, key, event=None, ext="harbor", **fields):
    r = env["api"].put(f"/inbound/v1/sources/{source}/events/{ext}",
                       {**MEET, **(event or {}), **fields}, key=key)
    assert r["status"] in (200, 201), r["json"]
    return r


def live(env, source):
    r = env["api"].call("POST", f"{TEAMS}/{source}/live")
    assert r["status"] == 200, r["json"]


def two_teams(env, **second):
    """Herons (Jesse) sorts before Otters (Nadia); both live with the same meet."""
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk, notes="Bring a towel\nParking on Quay St")
    live(env, herons)
    push(env, otters, ok, notes="Bring a towel\nNadia swims 200 IM", **second)
    live(env, otters)
    return (herons, hk), (otters, ok)


def calendar(env, day=DAY) -> list[dict]:
    out = []
    for f in env["out"].rglob("*.ics"):
        ev = next(c for c in Calendar.from_ical(f.read_bytes()).walk() if c.name == "VEVENT")
        if ev.decoded("DTSTART").isoformat()[:10] == day:
            out.append({"title": str(ev["SUMMARY"]), "body": str(ev.get("DESCRIPTION", "")),
                        "uid": str(ev["UID"])})
    return out


def state(env, uid):
    conn = db.connect(env["path"])
    try:
        return repo.event_state(conn, uid)
    finally:
        conn.close()




def uids(env, day=DAY):
    return sorted(e["uid"] for e in calendar(env, day))


def conn_for(env):
    return db.connect(env["path"])


def run_publish(env, **kw):
    conn = conn_for(env)
    try:
        return merging.publish(conn, env["target"], now=NOW, secrets=env["secrets"], **kw)
    finally:
        conn.close()


def delete_meet(env, source, key, ext="harbor"):
    r = env["api"].call("DELETE", f"/inbound/v1/sources/{source}/events/{ext}",
                        headers={"X-Calsync-Key": key})
    assert r["status"] in (200, 204), r


def test_two_teams_at_one_meet_make_one_event_and_the_old_copies_go(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk, notes="Bring a towel\nParking on Quay St")
    push(env, otters, ok, notes="Bring a towel\nNadia swims 200 IM")
    live(env, herons)
    env["target"].calls.clear()
    live(env, otters)
    (event,) = calendar(env)
    assert event["uid"] == MERGED
    assert "J+N" in event["title"] and "Harbor Invitational" in event["title"]
    lines = event["body"].splitlines()
    assert lines.count("Bring a towel") == 1
    assert "Parking on Quay St" in lines and "Nadia swims 200 IM" in lines
    # The merged event is accepted before either copy is taken down.
    calls = env["target"].calls
    cancels = [i for i, (kind, _) in enumerate(calls) if kind == "cancel"]
    assert {calls[i][1] for i in cancels} == {HERONS_UID, OTTERS_UID}
    assert env["target"].at("upsert", MERGED) < min(cancels)


def test_the_merged_event_lives_under_a_hidden_companion_and_nothing_is_withheld(env):
    two_teams(env)
    conn = conn_for(env)
    row = conn.execute("SELECT source_id, merged, kids, withheld FROM event_state "
                       "WHERE uid = ?", (MERGED,)).fetchone()
    assert row["source_id"] == "merged-in-herons" and row["kids"] == "jesse,nadia"
    assert json.loads(row["merged"])["body"]
    assert conn.execute("SELECT COUNT(*) FROM event_state WHERE withheld IS NOT NULL"
                        ).fetchone()[0] == 0
    members = conn.execute("SELECT member_uid, merged_uid FROM merged_members "
                           "ORDER BY member_uid").fetchall()
    assert [(m[0], m[1]) for m in members] == [(HERONS_UID, MERGED), (OTTERS_UID, MERGED)]
    conn.close()


def test_a_second_team_arriving_later_converges_in_that_one_push(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk)
    live(env, herons)
    assert uids(env) == [HERONS_UID]
    push(env, otters, ok)
    live(env, otters)
    assert uids(env) == [MERGED]


def test_changing_one_teams_notes_rewrites_the_merged_event_from_its_sync_alone(env):
    (herons, hk), (otters, ok) = two_teams(env)
    env["target"].calls.clear()
    push(env, otters, ok, notes="Bring a towel\nNadia swims 100 free")
    (event,) = calendar(env)
    assert "Nadia swims 100 free" in event["body"] and "200 IM" not in event["body"]
    assert env["target"].calls == [("upsert", MERGED)]


def test_a_lower_id_joining_later_takes_over_and_the_old_copy_goes(env):
    otters, ok = team(env, "Otters", "nadia")
    push(env, otters, ok)
    live(env, otters)
    herons, hk = team(env, "Herons", "jesse")
    push(env, herons, hk)
    live(env, herons)
    (event,) = calendar(env)
    assert "J+N" in event["title"] and event["uid"] == MERGED
    assert state(env, OTTERS_UID).cancelled


def test_a_team_cancelling_its_meet_restores_the_others_copy_before_the_merged_one_goes(env):
    (herons, hk), (otters, ok) = two_teams(env)
    env["target"].calls.clear()
    delete_meet(env, otters, ok)
    (event,) = calendar(env)
    assert event["uid"] == HERONS_UID
    assert "Jesse" in event["title"] and "Nadia" not in event["title"]
    assert "Nadia swims 200 IM" not in event["body"]
    assert env["target"].at("upsert", HERONS_UID) < env["target"].at("cancel", MERGED)
    conn = conn_for(env)
    assert conn.execute("SELECT COUNT(*) FROM merged_members").fetchone()[0] == 0
    conn.close()


def test_the_carrier_cancelling_hands_the_meet_back_to_the_other_team(env):
    (herons, hk), (otters, ok) = two_teams(env)
    env["target"].calls.clear()
    delete_meet(env, herons, hk)
    (event,) = calendar(env)
    assert event["uid"] == OTTERS_UID
    assert "Nadia" in event["title"] and "Jesse" not in event["title"]
    assert env["target"].at("upsert", OTTERS_UID) < env["target"].at("cancel", MERGED)


def test_a_group_end_keeps_the_merged_event_if_a_copy_cannot_be_restored(env):
    (herons, hk), (otters, ok) = two_teams(env)
    env["target"].fail = ("in-herons.",)
    delete_meet(env, otters, ok)
    assert uids(env) == [MERGED]               # nobody's event is missing
    conn = conn_for(env)
    assert HERONS_UID in [r[0] for r in conn.execute("SELECT member_uid FROM merged_members")]
    conn.close()
    env["target"].fail = ()
    report = run_publish(env)
    assert not report.errors
    assert uids(env) == [HERONS_UID]


def test_a_failed_merged_write_leaves_both_copies_on_the_calendar(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk)
    push(env, otters, ok)
    live(env, herons)
    env["target"].fail = ("merged.",)
    live(env, otters)
    assert uids(env) == [HERONS_UID, OTTERS_UID]
    conn = conn_for(env)
    assert conn.execute("SELECT COUNT(*) FROM merged_members").fetchone()[0] == 0
    conn.close()
    env["target"].fail = ()
    assert not run_publish(env).errors
    assert uids(env) == [MERGED]


def test_a_failed_merged_cancel_leaves_a_duplicate_not_a_gap(env):
    (herons, hk), (otters, ok) = two_teams(env)
    env["target"].fail = ("merged.",)
    delete_meet(env, otters, ok)
    assert uids(env) == sorted([HERONS_UID, MERGED])
    env["target"].fail = ()
    assert not run_publish(env).errors
    assert uids(env) == [HERONS_UID]


def test_publish_never_fetches(env):
    (herons, hk), (otters, ok) = two_teams(env)

    def boom(url):
        raise AssertionError("publish fetched")

    conn = conn_for(env)
    conn.execute("UPDATE shared_candidates SET body = 'New note' WHERE source_id = ?", (otters,))
    conn.commit()
    conn.close()
    report = run_publish(env, fetcher=boom)
    assert not report.errors and report.written == 1
    assert "New note" in calendar(env)[0]["body"]


def meets(env, source, key, n):
    for i in range(1, n + 1):
        push(env, source, key, ext=f"g{i}", label=f"Gala {i}",
             start=f"2026-12-{3 * i:02d}", end=f"2026-12-{3 * i + 1:02d}")


def on_calendar(env) -> int:
    return len(list(env["out"].rglob("*.ics")))


def five_merged(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    for source, key in ((herons, hk), (otters, ok)):
        meets(env, source, key, 5)
        live(env, source)
    assert on_calendar(env) == 5
    return herons, otters


def test_a_mass_group_end_gives_every_member_its_copy_back_before_cancelling(env):
    herons, otters = five_merged(env)
    conn = conn_for(env)
    conn.execute("DELETE FROM shared_candidates WHERE source_id = ?", (otters,))
    conn.commit()
    conn.close()
    env["target"].calls.clear()
    report = run_publish(env)
    assert report.ended == 5, report.errors
    t = env["target"]
    for i in range(1, 6):
        own = f"{herons}.g{i}@inbound.calsync"
        assert t.at("upsert", own) < t.at("cancel", f"merged.{own}")
    assert on_calendar(env) == 5
    conn = conn_for(env)
    assert conn.execute("SELECT COUNT(*) FROM merged_members").fetchone()[0] == 0
    conn.close()


def test_withholding_the_merged_event_takes_it_off_and_keeps_the_members_dropped(env):
    two_teams(env)
    r = env["api"].call("POST", STATUS, {"uid": MERGED, "status": "not_attending"})
    assert r["status"] == 200 and r["json"]["on_calendar"] is False, r["json"]
    assert calendar(env) == []
    conn = conn_for(env)
    assert [r[0] for r in conn.execute(
        "SELECT uid FROM event_state WHERE withheld IS NOT NULL")] == [MERGED]
    # A poll of either team must not put its own copy back.
    for source in ("in-herons", "in-otters"):
        sync_source(conn, repo.get_source(conn, source), env["target"], now=NOW,
                    secrets=env["secrets"])
    conn.close()
    assert calendar(env) == []
    r = env["api"].call("POST", STATUS, {"uid": MERGED, "status": None})
    assert r["status"] == 200, r["json"]
    assert uids(env) == [MERGED]


def test_the_console_withholds_and_restores_a_merged_event(env):
    two_teams(env)
    r = env["console"].post("/events/withhold", {"uid": MERGED, "reason": "cancelled"})
    assert r["status"] in (302, 303), r
    assert "1 event(s) off the calendar" in unquote(r["headers"]["Location"])
    assert calendar(env) == []
    r = env["console"].post("/events/restore", {"uid": MERGED})
    assert r["status"] in (302, 303), r
    assert uids(env) == [MERGED]


def test_a_kid_marked_out_of_the_merged_event_leaves_its_title(env):
    two_teams(env)
    r = env["api"].call("POST", STATUS, {"uid": MERGED, "status": "not_attending",
                                         "child": "nadia"})
    assert r["status"] == 200, r["json"]
    (event,) = calendar(env)
    assert "Jesse" in event["title"] and "Nadia" not in event["title"]
    assert "Nadia swims 200 IM" not in event["body"]
    conn = conn_for(env)
    assert conn.execute("SELECT child_id FROM event_absences WHERE uid = ?",
                        (OTTERS_UID,)).fetchone()[0] == "nadia"
    conn.close()


def test_a_kid_who_is_not_a_member_is_refused(env):
    two_teams(env)
    r = env["api"].call("POST", STATUS, {"uid": MERGED, "status": "not_attending",
                                         "child": "sam"})
    assert r["status"] == 422, r["json"]
    assert uids(env) == [MERGED]


def test_the_console_kid_route_works_on_a_merged_event_and_refuses_a_stranger(env):
    two_teams(env)
    r = env["console"].post("/events/withhold", {"uid": MERGED, "reason": "not_attending",
                                                 "child": "sam"})
    assert r["status"] >= 400 and "not on a team" in r["body"]
    r = env["console"].post("/events/withhold", {"uid": MERGED, "reason": "not_attending",
                                                 "child": "nadia"})
    assert r["status"] in (302, 303), r
    (event,) = calendar(env)
    assert "Nadia" not in event["title"]
    # Nadia's team was the whole of her side of the meet, so the group ended;
    # putting her back goes through her own team's copy.
    env["console"].post("/events/restore", {"uid": OTTERS_UID, "child": "nadia"})
    assert uids(env) == [MERGED]


def test_withholding_a_members_copy_leaves_the_other_kids_event(env):
    two_teams(env)
    r = env["api"].call("POST", STATUS, {"uid": HERONS_UID, "status": "cancelled"})
    assert r["status"] == 200, r["json"]
    (event,) = calendar(env)
    assert event["uid"] == OTTERS_UID
    assert "Nadia" in event["title"] and "Jesse" not in event["title"]


def test_a_held_member_poll_keeps_its_candidates_and_a_confirmed_one_refreshes_them(env):
    (herons, hk), _ = two_teams(env)
    for i in range(1, 6):
        push(env, herons, hk, ext=f"g{i}", label=f"Gala {i}",
             start=f"2026-12-{3 * i:02d}", end=f"2026-12-{3 * i + 1:02d}")
    conn = conn_for(env)
    stored = repo.source_candidates(conn, herons)
    payload = json.loads(repo.inbound_payload(conn, herons))
    payload["items"] = [i for i in payload["items"] if i["external_id"] == "g1"]
    raw = json.dumps(payload).encode()
    source = repo.get_source(conn, herons)
    report = sync_source(conn, source, env["target"], now=NOW, raw=raw)
    assert report.status == "held" and report.held_fingerprint
    assert repo.source_candidates(conn, herons) == stored
    assert MERGED in uids(env)
    done = sync_source(conn, source, env["target"], now=NOW, raw=raw,
                       confirm_held=report.held_fingerprint)
    assert done.status == "ok"
    assert [r["uid"] for r in repo.source_candidates(conn, herons)] == [
        r["uid"] for r in stored if r["uid"].startswith("in-herons.g1")]
    conn.close()


def test_a_paused_member_keeps_the_group(env):
    (herons, hk), (otters, ok) = two_teams(env)
    conn = conn_for(env)
    conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (herons,))
    conn.commit()
    sync_source(conn, repo.get_source(conn, otters), env["target"], now=NOW,
                secrets=env["secrets"])
    assert repo.merged_away(conn) == {HERONS_UID, OTTERS_UID}
    assert len(repo.source_candidates(conn, herons)) == 1
    conn.close()
    assert uids(env) == [MERGED]


def test_retiring_the_carrier_gives_the_other_team_its_own_copy_back(env):
    (herons, hk), (otters, ok) = two_teams(env)
    env["target"].calls.clear()
    r = env["console"].post(f"/sources/{herons}/retire", {})
    assert r["status"] in (302, 303), r
    (event,) = calendar(env)
    assert event["uid"] == OTTERS_UID
    assert "Nadia" in event["title"] and "Jesse" not in event["title"]
    assert env["target"].at("upsert", OTTERS_UID) < env["target"].at("cancel", MERGED)
    conn = conn_for(env)
    assert repo.source_candidates(conn, herons) == []
    conn.close()


def test_a_new_carrier_is_written_before_the_old_merged_event_goes(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    pike, pk = team(env, "Pike", "sam")
    for source, key in ((herons, hk), (otters, ok), (pike, pk)):
        push(env, source, key)
        live(env, source)
    (event,) = calendar(env)
    assert event["uid"] == MERGED and "Kids" in event["title"]
    env["target"].calls.clear()
    env["console"].post(f"/sources/{herons}/retire", {})
    (event,) = calendar(env)
    assert event["uid"] == f"merged.{OTTERS_UID}" and "N+S" in event["title"]
    assert env["target"].at("upsert", f"merged.{OTTERS_UID}") < env["target"].at("cancel", MERGED)


def test_a_claimed_copy_with_no_row_gets_a_cancelled_placeholder(env):
    two_teams(env)
    conn = conn_for(env)
    conn.execute("DELETE FROM event_state WHERE uid = ?", (OTTERS_UID,))
    conn.commit()
    sync_source(conn, repo.get_source(conn, "in-otters"), env["target"], now=NOW,
                secrets=env["secrets"])
    row = conn.execute("SELECT cancelled, withheld FROM event_state WHERE uid = ?",
                       (OTTERS_UID,)).fetchone()
    conn.close()
    assert tuple(row) == (1, None)
    assert uids(env) == [MERGED]


def test_a_merge_failure_never_changes_the_teams_status(env):
    (herons, hk), (otters, ok) = two_teams(env)
    env["target"].fail = ("merged.",)
    conn = conn_for(env)
    # The other team's notes moved, so this sync's publish has a write to make.
    conn.execute("UPDATE shared_candidates SET body = 'New note' WHERE source_id = ?", (herons,))
    conn.commit()
    report = sync_source(conn, repo.get_source(conn, otters), env["target"], now=NOW,
                         secrets=env["secrets"])
    assert report.status == "ok" and not report.errors and report.merge_errors
    assert conn.execute("SELECT last_error FROM sources WHERE id = ?", (otters,)
                        ).fetchone()[0] is None
    conn.close()


def test_a_merge_step_that_raises_is_reported_apart_from_the_team(env, monkeypatch):
    (herons, hk), (otters, ok) = two_teams(env)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(merging, "publish", boom)
    conn = conn_for(env)
    report = sync_source(conn, repo.get_source(conn, otters), env["target"], now=NOW,
                         secrets=env["secrets"])
    conn.close()
    assert report.status == "ok" and report.merge_errors == ["boom"]


def test_companions_are_hidden_and_never_synced(env):
    two_teams(env)
    conn = conn_for(env)
    assert [s.id for s in repo.list_sources(conn, enabled_only=False)] == [
        "in-herons", "in-otters"]
    ids = [s.id for s in repo.list_sources(conn, enabled_only=False, include_merged=True)]
    assert ids == ["in-herons", "in-otters", "merged-in-herons"]
    assert repo.get_source(conn, "merged-in-herons").kind == "merged"
    reports = sync_module.sync_all(conn, env["target"], now=NOW, secrets=env["secrets"])
    assert [r.source_id for r in reports] == ["in-herons", "in-otters"]
    with pytest.raises(AssertionError):
        sync_source(conn, repo.get_source(conn, "merged-in-herons"), env["target"], now=NOW)
    conn.close()
    teams = env["api"].call("GET", TEAMS)["json"]
    assert "merged-in-herons" not in json.dumps(teams)
    assert "merged-in-herons" not in env["console"].get("/")["body"]
    assert env["console"].get("/sources/merged-in-herons")["status"] >= 400


def test_the_fix_page_points_a_merged_event_back_to_the_kids_own_team(env):
    two_teams(env)
    r = env["console"].get(f"/events/fix?uid={MERGED}&back=/calendar")
    assert "own team" in unquote(r["headers"].get("Location", "")) + r["body"]


def test_a_correction_on_a_members_copy_flows_into_the_merged_event(env):
    two_teams(env)
    conn = conn_for(env)
    row = conn.execute("SELECT content FROM shared_candidates WHERE uid = ?",
                       (HERONS_UID,)).fetchone()
    conn.close()
    assert json.loads(row[0])["uid"] == HERONS_UID and json.loads(row[0])["venue_name"]


def test_placements_report_claimed_copies_as_merged(env):
    two_teams(env)
    conn = conn_for(env)
    states = {p["uid"]: p["state"] for p in repo.placements(conn, since="2000-01-01")}
    conn.close()
    assert states == {MERGED: "live", HERONS_UID: "merged", OTTERS_UID: "merged"}


def test_a_dry_run_writes_nothing(env):
    herons, hk = team(env, "Herons", "jesse")
    push(env, herons, hk)
    live(env, herons)
    conn = conn_for(env)
    conn.execute("DELETE FROM shared_candidates")
    conn.commit()
    report = sync_source(conn, repo.get_source(conn, herons), NoTarget(), now=NOW,
                         secrets=env["secrets"], dry_run=True)
    assert report.status == "ok"
    for table in ("shared_candidates", "merged_members"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert repo.companions(conn) == []
    conn.close()


def test_a_dry_run_leaves_a_merged_group_alone(env):
    (herons, hk), (otters, ok) = two_teams(env)
    conn = conn_for(env)
    before = [conn.execute(f"SELECT * FROM {t}").fetchall() for t in
              ("shared_candidates", "merged_members", "event_state")]
    sync_source(conn, repo.get_source(conn, otters), NoTarget(), now=NOW,
                secrets=env["secrets"], dry_run=True)
    after = [conn.execute(f"SELECT * FROM {t}").fetchall() for t in
             ("shared_candidates", "merged_members", "event_state")]
    conn.close()
    assert [[tuple(r) for r in t] for t in before] == [[tuple(r) for r in t] for t in after]


def test_practices_never_merge(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    for source, key in ((herons, hk), (otters, ok)):
        push(env, source, key, type="practice", label="Swim")
        live(env, source)
    assert len(calendar(env)) == 2


def test_a_different_venue_keeps_two_events(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk)
    push(env, otters, ok, location="Ridgeway Natatorium")
    live(env, herons)
    live(env, otters)
    assert len(calendar(env)) == 2


def test_venue_and_field_conflicts_are_found_and_silence_is_no_opinion():
    row = lambda venue, field: {"venue": venue, "field": field}  # noqa: E731
    assert correlate.venues_conflict([row("Wrenfield", None), row("Ridgeway", None)])
    assert correlate.venues_conflict([row("Wrenfield", "#1"), row("wrenfield", "#2")])
    assert not correlate.venues_conflict([row("Wrenfield", None), row("WRENFIELD", None)])
    assert not correlate.venues_conflict([row("Wrenfield", "#1"), row(None, None)])


def test_timed_never_matches_all_day(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk)
    push(env, otters, ok, start="2026-11-14T09:00:00", end="2026-11-14T12:00:00")
    live(env, herons)
    live(env, otters)
    assert len(calendar(env)) == 2


def test_a_staged_source_never_joins_a_group(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk)
    live(env, herons)
    push(env, otters, ok)                      # still staged
    conn = conn_for(env)
    assert repo.source_candidates(conn, otters) == []
    conn.close()
    herons_event = next(e for e in calendar(env) if e["uid"] == HERONS_UID)
    assert "J+N" not in herons_event["title"]
    assert MERGED not in uids(env)


def test_names_match_after_normalising_and_practices_have_no_key():
    from calsync.models import Event, Venue  # noqa: F401
    mk = lambda **kw: Event(  # noqa: E731
        uid="u", activity_id="a", starts_at=datetime(2026, 11, 14, 5, tzinfo=timezone.utc),
        ends_at=datetime(2026, 11, 15, 5, tzinfo=timezone.utc), is_game=True,
        tz="America/New_York", all_day=True, **kw)
    assert correlate.key(mk(detail="Harbor  Invitational!"), "swimming", "c") == \
        correlate.key(mk(detail="harbor invitational", opponent=""), "swimming", "c")
    assert correlate.key(mk(detail="A"), "swimming", "c") != correlate.key(mk(detail="A"), "diving", "c")
    assert correlate.key(mk(detail="A"), "swimming", "c") != correlate.key(mk(detail="A"), "swimming", "d")
    assert correlate.key(mk(warmup_for="x"), "swimming", "c") is None


@pytest.mark.parametrize("template", ["{child}-{type}", "{activity}-{type}"])
def test_copies_bound_for_different_calendars_are_not_merged(env, template):
    conn = conn_for(env)
    set_setting(conn, "collection_template", template)
    conn.close()
    two_teams(env)
    events = calendar(env)
    assert len(events) == 2
    assert all("J+N" not in e["title"] for e in events)


def reader(env, tmp_path):
    token = tmp_path / "read.json"
    token.write_text(json.dumps({"api_token": "tok"}))
    token.chmod(0o600)
    return ReadClient(api_app.create_app(env["path"], secrets=SecretStore(
        path=token, environ={}), clock=lambda: NOW), token="tok")


def test_the_read_api_leaves_claimed_copies_out_and_points_them_at_the_merged_event(env, tmp_path):
    two_teams(env)
    read = reader(env, tmp_path)
    listed = read.get("/v1/events?from=2026-11-01&to=2026-11-30")["json"]
    assert [e["uid"] for e in listed["events"]] == [MERGED]
    assert [c["id"] for c in listed["events"][0]["children"]] == ["jesse", "nadia"]
    household = env["api"].get("/inbound/v1/calendar?from=2026-11-01&to=2026-11-30")["json"]
    assert [e["uid"] for e in household["events"]] == [MERGED]
    for uid in (HERONS_UID, OTTERS_UID):
        copy = read.get(f"/v1/events/{uid}")["json"]["event"]
        assert copy["cancelled"] is False and copy["merged_into"] == MERGED
    merged = read.get(f"/v1/events/{MERGED}")["json"]["event"]
    assert merged["merged_into"] is None
    assert "Nadia swims 200 IM" in merged["notes"] and "Parking on Quay St" in merged["notes"]
    conn = conn_for(env)
    assert "Nadia swims" not in conn.execute(
        "SELECT body FROM event_content WHERE uid = ?", (MERGED,)).fetchone()[0]
    conn.close()


TIMED = {"start": "2026-11-14T09:00:00", "end": "2026-11-14T12:00:00"}


def test_the_merged_warmup_starts_at_the_earliest_arrival(env, tmp_path):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    conn = conn_for(env)
    for source, minutes in ((herons, 45), (otters, 90)):
        conn.execute("UPDATE activities SET warmup_minutes = ? WHERE id = "
                     "(SELECT activity_id FROM sources WHERE id = ?)", (minutes, source))
    conn.commit()
    conn.close()
    for source, key in ((herons, hk), (otters, ok)):
        push(env, source, key, **TIMED)
        live(env, source)
    warm = f"calsync-warmup-{MERGED}"
    assert uids(env) == sorted([MERGED, warm])
    conn = conn_for(env)
    row = conn.execute("SELECT shown_starts_at FROM event_state WHERE uid = ?", (warm,)).fetchone()
    conn.close()
    assert datetime.fromisoformat(row[0]) == (
        datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc) - timedelta(minutes=90))
    read = reader(env, tmp_path)
    one = read.get(f"/v1/events/{MERGED}")["json"]["event"]
    assert one["arrive_at"].startswith("2026-11-14T07:30")
    # Both old copies and both old warm-ups are gone from the calendar.
    assert state(env, f"calsync-warmup-{HERONS_UID}").cancelled


def test_a_team_leaving_a_group_that_lives_on_gets_its_own_copy_back(env):
    teams = [team(env, name, kid) for name, kid in
             (("Herons", "jesse"), ("Otters", "nadia"), ("Wrens", "sam"))]
    for source, key in teams:
        push(env, source, key)
        live(env, source)
    assert len(calendar(env)) == 1
    wrens, wk = teams[2]
    push(env, wrens, wk, start="2026-11-14T09:00:00", end="2026-11-14T11:00:00")
    days = calendar(env)
    assert len(days) == 2, days
    assert any(e["uid"].startswith(wrens + ".") for e in days)


def test_lifting_a_withhold_after_the_group_ended_brings_the_copy_straight_back(env):
    (herons, hk), (otters, ok) = two_teams(env)
    r = env["api"].call("POST", STATUS, {"uid": MERGED, "status": "not_attending"})
    assert r["status"] == 200 and calendar(env) == []
    delete_meet(env, otters, ok)
    assert env["api"].call("POST", STATUS, {"uid": MERGED, "status": None})["status"] == 200
    (event,) = calendar(env)
    assert event["uid"].startswith(herons + ".")


def test_the_api_names_the_carrier_team_and_never_calls_a_merged_event_stale(env, tmp_path):
    two_teams(env)
    body = reader(env, tmp_path).get(f"/v1/events/{MERGED}")["json"]
    assert [s["id"] for s in body["sources"]] == ["in-herons"]
    assert not any(s["stale"] for s in body["sources"])


def test_a_paused_feed_is_not_fetched_to_end_a_group_and_the_merged_event_waits(env):
    (herons, hk), (otters, ok) = two_teams(env)
    conn = conn_for(env)
    conn.execute("UPDATE sources SET kind = 'teamreach', enabled = 0, "
                 "url_template = 'https://feeds.example/otters.ics' WHERE id = ?", (otters,))
    conn.execute("DELETE FROM shared_candidates WHERE source_id = ?", (herons,))
    conn.commit()
    conn.close()
    fetched = []
    report = run_publish(env, fetcher=lambda url: fetched.append(url) or b"")
    assert fetched == []
    assert report.ended == 0 and any("could not be restored" in e for e in report.errors)
    assert [e["uid"] for e in calendar(env)] == [MERGED]


def test_unmerge_gives_every_team_its_copy_back_and_disables_the_companions(env):
    (herons, hk), (otters, ok) = two_teams(env)
    conn = conn_for(env)
    report = merging.unmerge(conn, env["target"], now=NOW, secrets=env["secrets"])
    assert report.ended == 1 and report.errors == []
    assert sorted(e["uid"].split(".")[0] for e in calendar(env)) == [herons, otters]
    assert [s.enabled for s in repo.companions(conn)] == [False]
    # 0.21.0 lists enabled sources only, so it never polls the companion.
    assert all(s.kind != "merged" for s in repo.list_sources(conn))
    conn.close()


def test_unmerge_keeps_a_withheld_merged_meet_off(env):
    two_teams(env)
    env["api"].call("POST", STATUS, {"uid": MERGED, "status": "not_attending"})
    conn = conn_for(env)
    merging.unmerge(conn, env["target"], now=NOW, secrets=env["secrets"])
    conn.close()
    assert calendar(env) == []
