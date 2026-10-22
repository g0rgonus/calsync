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


def revoke(env, confirm="yes", **extra):
    return env["console"].post("/settings/kill-switch", {"confirm": confirm, **extra})


def working_keys(env) -> int:
    conn = db.connect(env["path"])
    count = len(repo.api_keys(conn))
    conn.close()
    return count


def test_revoking_every_key_and_each_service_says_so(env):
    assert "Revoke every key" in env["console"].get("/settings")["body"]
    assert revoke(env, " Yes ")["status"] == 303
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
    revoke(env)
    page = env["console"].post("/sources/in-herons/inbound-key")["body"]
    fresh = re.search(r"csk_[A-Za-z0-9_-]{20,}", page).group(0)
    assert env["inbound"].get("/inbound/v1/sources/in-herons", key=fresh)["status"] == 200


@pytest.mark.parametrize("confirm", ["", "no", "y"])
def test_anything_but_yes_revokes_nothing(env, confirm):
    assert revoke(env, confirm)["status"] >= 400
    assert working_keys(env) == 3 and env["sent"] == []


def test_feed_teams_keep_syncing_unless_asked(env):
    revoke(env, pause_feeds="1")
    conn = db.connect(env["path"])
    enabled = {s.id: s.enabled for s in repo.list_sources(conn, enabled_only=False)}
    conn.close()
    assert enabled == {"in-herons": True, "tr-wrens": False}


def test_paused_feed_teams_are_listed_with_a_resume_button(env):
    revoke(env, pause_feeds="1")
    settings = env["console"].get("/settings")["body"]
    assert 'action="/sources/tr-wrens/enabled"' in settings and "Resume" in settings
    env["console"].post("/sources/tr-wrens/enabled", {"enabled": "1"})
    assert "tr-wrens (running again)" in env["console"].get("/settings")["body"]


def test_firing_again_with_nothing_working_is_refused_and_keeps_the_list(env):
    revoke(env)
    again = revoke(env)
    assert again["status"] >= 400 and "nothing to revoke" in again["body"]
    assert "Grok Bot" in env["console"].get("/settings")["body"]
    assert len(env["sent"]) == 1


def test_a_read_token_that_cannot_be_read_is_reported_not_blocked(env, tmp_path):
    no_token = tmp_path / "no-token.json"
    no_token.write_text(json.dumps({"pushover_token": "t", "pushover_user": "u"}))
    no_token.chmod(0o600)
    sent = []

    class Push:
        def __call__(self, request, timeout=None):
            sent.append(request.data.decode())
            return _PushReply()

    console = ConsoleClient(web_app.create_app(
        env["path"], secrets=SecretStore(path=no_token, environ={}),
        clock=lambda: NOW, push_opener=Push()))
    console.post("/settings/kill-switch", {"confirm": "yes"})
    assert "could not be read, so it was not blocked" in console.get("/settings")["body"]
    assert "NOT+blocked" in sent[0]              # the push body is form-encoded


def test_revoked_is_said_only_where_the_key_once_worked(env):
    revoke(env)
    own = env["inbound"].get("/inbound/v1/sources/in-herons", key=TEAM_KEY)
    assert own["json"]["error"]["code"] == "revoked"
    elsewhere = env["inbound"].get("/inbound/v1/calendar", key=TEAM_KEY)
    assert elsewhere["status"] == 401
    assert elsewhere["json"]["error"]["code"] == "bad_credential"
    admin_route = env["inbound"].get("/inbound/v1/admin/teams", key=READ_KEY)
    assert admin_route["json"]["error"]["code"] == "bad_credential"
