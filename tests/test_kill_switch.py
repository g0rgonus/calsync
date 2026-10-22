"""Revoking every key at once from the console, and what each service says after."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from calsync import db, repo
from calsync.api import app as api_app
from calsync.inbound import app as inbound_app
from calsync.inbound import events as inbound
from calsync.secrets import SecretStore
from calsync.web import app as web_app

from test_inbound import Client as ApiClient
from test_web import Client as ConsoleClient, _PushReply

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"
TEAM_KEY = "csk_test-key-for-the-herons-team-00000000"
READ_KEY = "csk_test-household-read-key-0000000000000"
ADMIN_KEY = "csk_test-admin-key-for-a-tool-000000000000"
API_TOKEN = "test-read-api-token"


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "calsync.db"
    conn = db.open_db(path)
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order) VALUES ('nadia', 'Nadia', 'N', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz, alarm_game_min,
                                alarm_practice_min)
             VALUES ('nadia-swim-herons', 'nadia', 'Herons', 'swimming', 'UTC', 90, 30),
                    ('nadia-soccer-wrens', 'nadia', 'Wrens', 'soccer', 'UTC', 90, 30);
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('in-herons', 'nadia-swim-herons', 'inbound', 'push'),
                    ('tr-wrens', 'nadia-soccer-wrens', 'teamreach', 'feed');
        """
    )
    repo.set_inbound_key_hash(conn, "in-herons", inbound.hash_key(TEAM_KEY))
    repo.set_read_key_hash(conn, inbound.hash_key(READ_KEY))
    repo.issue_admin_key(conn, "Grok Bot", inbound.hash_key(ADMIN_KEY))
    conn.close()

    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"api_token": API_TOKEN, "pushover_token": "t",
                                        "pushover_user": "u"}))
    secrets_file.chmod(0o600)
    secrets = SecretStore(path=secrets_file, environ={})
    clock = {"now": NOW}
    sent = []

    class Push:
        def __call__(self, request, timeout=None):
            sent.append(request.data.decode())
            return _PushReply()

    console = ConsoleClient(web_app.create_app(
        path, secrets=secrets, clock=lambda: clock["now"], push_opener=Push()))
    services = ApiClient(inbound_app.create_app(
        path, secrets=secrets, clock=lambda: clock["now"], log=lambda line: None))
    read_api = ApiClient(api_app.create_app(path, secrets=secrets, clock=lambda: NOW))
    return {"path": path, "console": console, "inbound": services, "api": read_api,
            "clock": clock, "sent": sent}


def set_password(console, new=PASSWORD, current=None):
    data = {"new": new, "confirm": new}
    if current is not None:
        data["current"] = current
    return console.post("/settings/admin-password", data)


def revoke(env, password=PASSWORD, **extra):
    return env["console"].post("/settings/kill-switch", {"password": password, **extra})


def working_keys(env) -> int:
    conn = db.connect(env["path"])
    count = len(repo.api_keys(conn))
    conn.close()
    return count


def test_revoking_every_key_and_each_service_says_so(env):
    assert set_password(env["console"])["status"] == 303
    page = env["console"].get("/settings/kill-switch")
    assert page["status"] == 200 and "Grok Bot" in page["body"]
    assert revoke(env)["status"] == 303
    assert working_keys(env) == 0

    team = env["inbound"].get("/inbound/v1/sources/in-herons", key=TEAM_KEY)
    assert team["status"] == 401 and team["json"]["error"]["code"] == "revoked"
    calendar = env["inbound"].get("/inbound/v1/calendar", key=READ_KEY)
    assert calendar["json"]["error"]["code"] == "revoked"
    admin = env["inbound"].get("/inbound/v1/admin/teams", key=ADMIN_KEY)
    assert admin["json"]["error"]["code"] == "revoked"
    api = env["api"].call("GET", "/v1/events", key=None,
                          headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert api["status"] == 401 and api["json"]["error"]["code"] == "revoked"

    assert len(env["sent"]) == 1 and "revoked" in env["sent"][0]
    settings = env["console"].get("/settings")["body"]
    assert "Every key was revoked" in settings
    assert "Grok Bot" in settings and "/sources/in-herons" in settings
    assert "Every key was revoked" in env["console"].get("/")["body"]

    env["console"].post("/settings/recovery/dismiss")
    assert "Every key was revoked" not in env["console"].get("/settings")["body"]


def test_a_new_key_works_after_the_revoke(env):
    set_password(env["console"])
    revoke(env)
    page = env["console"].post("/sources/in-herons/inbound-key")["body"]
    fresh = re.search(r"csk_[A-Za-z0-9_-]{20,}", page).group(0)
    assert env["inbound"].get("/inbound/v1/sources/in-herons", key=fresh)["status"] == 200


def test_a_wrong_password_revokes_nothing(env):
    set_password(env["console"])
    r = revoke(env, "wrong password")
    assert r["status"] == 403 and "Nothing was revoked" in r["body"]
    assert working_keys(env) == 3 and env["sent"] == []


def test_no_password_no_kill_switch(env):
    assert "Set an admin password" in env["console"].get("/settings/kill-switch")["body"]
    assert revoke(env)["status"] == 409
    assert working_keys(env) == 3


def test_feed_teams_keep_syncing_unless_asked(env):
    set_password(env["console"])
    revoke(env, pause_feeds="1")
    conn = db.connect(env["path"])
    enabled = {s.id: s.enabled for s in repo.list_sources(conn, enabled_only=False)}
    conn.close()
    assert enabled == {"in-herons": True, "tr-wrens": False}


def test_changing_the_password_needs_the_current_one(env):
    assert set_password(env["console"])["status"] == 303
    assert set_password(env["console"], "a new long password")["status"] >= 400
    assert set_password(env["console"], "a new long password",
                        current="wrong")["status"] >= 400
    assert set_password(env["console"], "a new long password",
                        current=PASSWORD)["status"] == 303
    assert set_password(env["console"], "short", current="a new long password")[
        "status"] >= 400
    conn = db.connect(env["path"])
    stored = repo.setting_row(conn, repo.ADMIN_PASSWORD)
    conn.close()
    assert "a new long password" not in stored
