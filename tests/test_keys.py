"""Key storage in `api_keys` and `admin_log`. Auth itself is in test_inbound."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from calsync import db, repo
from calsync.inbound import events as inbound


@pytest.fixture
def conn(tmp_path):
    conn = db.open_db(tmp_path / "calsync.db")
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('nadia', 'Nadia', 'N', 1);
        INSERT INTO activities (id, child_id, name, sport_id, tz,
                                alarm_game_min, alarm_practice_min)
             VALUES ('nadia-swim-herons', 'nadia', 'Herons', 'swimming',
                     'America/New_York', 90, 30);
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('in-herons', 'nadia-swim-herons', 'inbound', 'push');
        """
    )
    yield conn
    conn.close()


def _log(conn) -> list[tuple[str, str, dict]]:
    return [(r["actor"], r["action"], json.loads(r["detail"]))
            for r in reversed(repo.admin_log(conn))]


def test_keys_kept_by_v13_are_moved_and_keep_working(conn):
    conn.execute("UPDATE sources SET inbound_key_sha256 = 'team-hash', "
                 "inbound_key_used_at = '2026-10-01T00:00:00+00:00'")
    conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)",
                     [("inbound_read_key_sha256", "read-hash"),
                      ("inbound_read_key_used_at", "2026-10-02T00:00:00+00:00")])
    conn.commit()

    db.migrate(conn)
    db.migrate(conn)   # rerun must not duplicate

    assert repo.inbound_key_hash(conn, "in-herons") == "team-hash"
    assert repo.inbound_key_used_at(conn, "in-herons") == "2026-10-01T00:00:00+00:00"
    assert repo.read_key_hash(conn) == "read-hash"
    assert repo.read_key_used_at(conn) == "2026-10-02T00:00:00+00:00"
    assert len(repo.api_keys(conn)) == 2
    old = conn.execute("SELECT inbound_key_sha256, inbound_key_used_at FROM sources").fetchone()
    assert tuple(old) == (None, None)
    assert not conn.execute("SELECT 1 FROM settings WHERE key LIKE 'inbound_read_key%'").fetchall()


def test_replacing_a_team_key_revokes_the_old_one_and_logs_both(conn):
    repo.set_inbound_key_hash(conn, "in-herons", inbound.hash_key("csk_first"))
    repo.touch_inbound_key(conn, "in-herons", "2026-10-03T00:00:00+00:00")
    repo.set_inbound_key_hash(conn, "in-herons", inbound.hash_key("csk_second"))

    assert repo.inbound_key_hash(conn, "in-herons") == inbound.hash_key("csk_second")
    assert repo.inbound_key_used_at(conn, "in-herons") is None
    everything = repo.api_keys(conn, include_revoked=True)
    assert [k.revoked_at is None for k in everything] == [False, True]
    assert [a for _, a, _ in _log(conn)] == ["key_issued", "key_revoked", "key_issued"]


def test_a_team_never_has_two_working_keys(conn):
    repo.set_inbound_key_hash(conn, "in-herons", "one")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO api_keys (scope, source_id, label, sha256) "
                     "VALUES ('team', 'in-herons', 'x', 'two')")


def test_removing_the_read_key_revokes_it(conn):
    repo.set_read_key_hash(conn, "read-hash")
    repo.set_read_key_hash(conn, None)
    assert repo.read_key_hash(conn) is None
    assert repo.api_keys(conn) == []
    assert len(repo.api_keys(conn, include_revoked=True)) == 1


def test_admin_keys_are_revoked_one_at_a_time(conn):
    grok = repo.issue_admin_key(conn, "Grok Bot", "grok-hash")
    repo.issue_admin_key(conn, "Dropbot", "drop-hash")

    assert repo.admin_key(conn, "grok-hash").label == "Grok Bot"
    assert repo.admin_key(conn, "team-hash") is None
    assert repo.revoke_key(conn, grok, actor="Dropbot")
    assert repo.admin_key(conn, "grok-hash") is None
    assert repo.admin_key(conn, "drop-hash") is not None
    assert not repo.revoke_key(conn, grok)            # already revoked
    assert _log(conn)[-1] == ("Dropbot", "key_revoked",
                              {"scope": "admin", "label": "Grok Bot"})


def test_a_team_key_cannot_open_the_admin_api(conn):
    repo.set_inbound_key_hash(conn, "in-herons", "team-hash")
    assert repo.admin_key(conn, "team-hash") is None


def test_deleting_a_team_takes_its_keys(conn):
    repo.set_inbound_key_hash(conn, "in-herons", "team-hash")
    conn.execute("DELETE FROM sources WHERE id = 'in-herons'")
    assert repo.api_keys(conn, include_revoked=True) == []


def test_a_change_that_rolls_back_leaves_no_log_entry(conn):
    repo.log_admin(conn, "Grok Bot", "team_created", "team:x")
    conn.rollback()
    assert repo.admin_log(conn) == []


@pytest.mark.parametrize("scope, source_id", [
    ("owner", None),            # not a scope
    ("team", None),             # a team key needs its team
    ("admin", "in-herons"),     # only a team key names one
])
def test_a_key_must_have_a_valid_scope(conn, scope, source_id):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO api_keys (scope, source_id, label, sha256) "
                     "VALUES (?, ?, 'x', 'h')", (scope, source_id))


def test_created_at_is_iso_like_the_other_timestamps(conn):
    repo.issue_admin_key(conn, "Grok Bot", "grok-hash")
    created = repo.api_keys(conn)[0].created_at
    assert datetime.fromisoformat(created).tzinfo is not None
