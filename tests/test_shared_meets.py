"""Kids on different teams at one meet: one event, titled and described for both.

Driven through the inbound API against one calendar on disk, like
test_shared_teams.py, so a title is an assertion about what a phone would show.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from icalendar import Calendar

from calsync import correlate, db, repo
from calsync.settings import set_setting
from calsync.inbound import app as inbound_app
from calsync.inbound import events as inbound
from calsync.secrets import SecretStore
from calsync.sync import NoTarget, sync_source
from calsync.targets import build

from test_inbound import Client as ApiClient

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ADMIN_KEY = "csk_test-admin-key-for-a-tool-000000000000"
TEAMS = "/inbound/v1/admin/teams"
STATUS = "/inbound/v1/admin/events/status"
MEET = {"type": "game", "label": "Harbor Invitational", "start": "2026-11-14",
        "end": "2026-11-15", "location": "Wrenfield Aquatic Center"}
DAY = "2026-11-14"


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
    repo.upsert_venue(conn, name="Ridgeway Natatorium")
    conn.close()
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"pushover_token": "t", "pushover_user": "u"}))
    secrets_file.chmod(0o600)
    secrets = SecretStore(path=secrets_file, environ={})
    target = build("ics_file", directory=tmp_path / "out")
    api = ApiClient(inbound_app.create_app(
        path, secrets=secrets, clock=lambda: NOW, log=lambda line: None,
        write_target=target, notify_sender=lambda c, s, message, **kw: None),
        key=ADMIN_KEY)
    return {"path": path, "api": api, "out": tmp_path / "out", "target": target,
            "secrets": secrets}


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


def test_two_teams_at_one_meet_make_one_event_for_both(env):
    two_teams(env)
    (event,) = calendar(env)
    assert "J+N" in event["title"] and "Harbor Invitational" in event["title"]
    lines = event["body"].splitlines()
    assert lines.count("Bring a towel") == 1
    assert "Parking on Quay St" in lines and "Nadia swims 200 IM" in lines


def test_a_second_push_after_the_first_was_written_converges_without_a_duplicate(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk)
    live(env, herons)
    assert [e["title"] for e in calendar(env)] == [calendar(env)[0]["title"]]
    assert "J+N" not in calendar(env)[0]["title"]
    push(env, otters, ok)
    live(env, otters)
    (event,) = calendar(env)
    assert "J+N" in event["title"]
    assert event["uid"].startswith("in-herons.")


def test_a_lower_id_joining_later_takes_over_and_the_old_copy_goes(env):
    otters, ok = team(env, "Otters", "nadia")
    push(env, otters, ok)
    live(env, otters)
    herons, hk = team(env, "Herons", "jesse")
    push(env, herons, hk)
    live(env, herons)
    (event,) = calendar(env)
    assert "J+N" in event["title"] and event["uid"].startswith("in-herons.")
    assert state(env, event["uid"].replace("in-herons.", "in-otters.")).cancelled


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


def test_a_team_that_cancels_leaves_the_other_titled_for_its_kid(env):
    (herons, hk), (otters, ok) = two_teams(env)
    assert env["api"].call("DELETE", f"/inbound/v1/sources/{otters}/events/harbor",
                           headers={"X-Calsync-Key": ok})["status"] in (200, 204)
    (event,) = calendar(env)
    assert "Jesse" in event["title"] and "Nadia" not in event["title"]
    assert "Nadia swims 200 IM" not in event["body"]


def test_the_carrier_cancelling_hands_the_event_to_the_other(env):
    (herons, hk), (otters, ok) = two_teams(env)
    assert env["api"].call("DELETE", f"/inbound/v1/sources/{herons}/events/harbor",
                           headers={"X-Calsync-Key": hk})["status"] in (200, 204)
    (event,) = calendar(env)
    assert "Nadia" in event["title"] and "Jesse" not in event["title"]
    assert event["uid"].startswith("in-otters.")


def test_a_kid_marked_out_of_their_copy_leaves_the_merged_title(env):
    (herons, hk), (otters, ok) = two_teams(env)
    uid = calendar(env)[0]["uid"]
    r = env["api"].call("POST", STATUS, {"uid": uid, "status": "not_attending",
                                         "child": "jesse"})
    assert r["status"] == 200, r["json"]
    # Jesse was Herons' only kid, so that copy is off and Otters carries alone.
    (event,) = calendar(env)
    assert "Nadia" in event["title"] and "Jesse" not in event["title"]


def test_a_staged_source_never_joins_a_group(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk)
    live(env, herons)
    push(env, otters, ok)                      # still staged
    conn = db.connect(env["path"])
    assert repo.source_candidates(conn, otters) == []
    assert "J+N" not in calendar(env)[0]["title"]
    conn.close()
    sources = [s for s in repo.list_sources(db.connect(env["path"])) if s.id == otters]
    assert sources[0].staging_collection


def test_a_disabled_source_is_never_the_carrier(env):
    (herons, hk), (otters, ok) = two_teams(env)
    conn = db.connect(env["path"])
    conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (herons,))
    conn.commit()
    assert repo.merged_away(conn) == set()
    groups = repo.candidate_groups(conn, {r["key"] for r in repo.source_candidates(conn, otters)})
    assert {r["source_id"] for rows in groups.values() for r in rows} == {otters}
    conn.close()


def test_placements_report_the_non_carrier_as_merged(env):
    two_teams(env)
    conn = db.connect(env["path"])
    states = {p["uid"].split(".")[0]: p["state"]
              for p in repo.placements(conn, since="2000-01-01")}
    conn.close()
    assert states == {"in-herons": "live", "in-otters": "merged"}


def test_a_dry_run_writes_no_candidates(env):
    herons, hk = team(env, "Herons", "jesse")
    push(env, herons, hk)
    live(env, herons)
    conn = db.connect(env["path"])
    conn.execute("DELETE FROM shared_candidates")
    conn.commit()
    source = repo.get_source(conn, herons)
    report = sync_source(conn, source, NoTarget(), now=NOW, secrets=env["secrets"],
                         dry_run=True)
    assert report.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM shared_candidates").fetchone()[0] == 0
    conn.close()


def test_dropping_a_copy_trips_no_guard_and_is_not_a_person_withholding(env):
    (herons, hk), (otters, ok) = two_teams(env)
    conn = db.connect(env["path"])
    source = repo.get_source(conn, otters)
    report = sync_source(conn, source, env["target"], now=NOW, secrets=env["secrets"])
    assert report.status == "ok" and report.held is None and not report.errors
    rows = conn.execute("SELECT uid, withheld, cancelled FROM event_state").fetchall()
    assert all(r["withheld"] is None for r in rows)
    conn.close()
    assert len(calendar(env)) == 1


def test_a_partner_is_synced_once_when_the_group_changes(env):
    herons, hk = team(env, "Herons", "jesse")
    otters, ok = team(env, "Otters", "nadia")
    push(env, herons, hk)
    live(env, herons)
    push(env, otters, ok)
    conn = db.connect(env["path"])
    conn.execute("UPDATE sources SET staging_collection = NULL WHERE id = ?", (otters,))
    conn.commit()
    report = sync_source(conn, repo.get_source(conn, otters), env["target"], now=NOW,
                         secrets=env["secrets"])
    assert report.partners_synced == [herons]
    again = sync_source(conn, repo.get_source(conn, otters), env["target"], now=NOW,
                        secrets=env["secrets"])
    assert again.partners_synced == []
    conn.close()


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


def test_a_held_poll_does_not_reshape_the_group(env):
    (herons, hk), _ = two_teams(env)
    for i in range(1, 6):
        push(env, herons, hk, ext=f"g{i}", label=f"Gala {i}",
             start=f"2026-12-{3 * i:02d}", end=f"2026-12-{3 * i + 1:02d}")
    conn = db.connect(env["path"])
    stored = repo.source_candidates(conn, herons)
    payload = json.loads(repo.inbound_payload(conn, herons))
    payload["items"] = [i for i in payload["items"] if i["external_id"] == "g1"]
    report = sync_source(conn, repo.get_source(conn, herons), env["target"], now=NOW,
                         raw=json.dumps(payload).encode())
    assert report.status == "held"
    assert repo.source_candidates(conn, herons) == stored
    conn.close()
    (event,) = calendar(env)
    assert "J+N" in event["title"]


@pytest.mark.parametrize("template", ["{child}-{type}", "{activity}-{type}"])
def test_copies_bound_for_different_calendars_are_not_merged(env, template):
    conn = db.connect(env["path"])
    set_setting(conn, "collection_template", template)
    conn.close()
    two_teams(env)
    events = calendar(env)
    assert len(events) == 2
    assert all("J+N" not in e["title"] for e in events)
