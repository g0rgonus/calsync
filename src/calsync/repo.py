"""Load domain objects out of the database.

The normalizers and adapters take plain dataclasses and don't know the DB
exists, which is what keeps them unit-testable without fixtures on disk. This
module is the only thing that bridges the two.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .models import Activity, Child, Venue


@dataclass(frozen=True)
class Source:
    id: str
    activity_id: str
    kind: str
    shape: str
    tier: int
    trust_rank: int
    poll_interval_s: int
    url_template: str | None
    secret_ref: str | None
    config: dict
    enabled: bool
    staging_collection: str | None = None


def get_child(conn: sqlite3.Connection, child_id: str) -> Child:
    row = conn.execute("SELECT * FROM children WHERE id = ?", (child_id,)).fetchone()
    if row is None:
        raise KeyError(f"no child {child_id!r}")
    return _child(row)


def _child(row: sqlite3.Row) -> Child:
    return Child(
        id=row["id"],
        name=row["name"],
        initial=row["initial"],
        birth_order=row["birth_order"],
        nicknames=tuple(json.loads(row["nicknames"] or "[]")),
    )


def list_children(conn: sqlite3.Connection) -> list[Child]:
    rows = conn.execute("SELECT * FROM children ORDER BY birth_order, name")
    return [_child(r) for r in rows]


def get_venue(conn: sqlite3.Connection, venue_id: int | None) -> Venue | None:
    if venue_id is None:
        return None
    row = conn.execute("SELECT * FROM venues WHERE id = ?", (venue_id,)).fetchone()
    if row is None:
        return None
    return Venue(
        raw=row["canonical_name"],
        name=row["canonical_name"],
        address=row["address"],
        lat=row["lat"],
        lon=row["lon"],
        pin_confirmed=bool(row["pin_confirmed"]),
    )


def get_activity(conn: sqlite3.Connection, activity_id: str) -> Activity:
    row = conn.execute(
        """
        SELECT a.*, s.id AS sport_key, s.emoji AS sport_emoji,
               v.canonical_name AS home_venue_name
          FROM activities a
          JOIN sports s ON s.id = a.sport_id
     LEFT JOIN venues v ON v.id = a.home_venue_id
         WHERE a.id = ?
        """,
        (activity_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no activity {activity_id!r}")

    aliases = tuple(
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM activity_aliases WHERE activity_id = ?", (activity_id,)
        )
    )
    return Activity(
        id=row["id"],
        child_id=row["child_id"],
        name=row["name"],
        sport=row["sport_key"],
        # Per-activity emoji wins, so two teams in one sport can differ.
        emoji=row["emoji"] or row["sport_emoji"],
        tz=row["tz"],
        official_name=row["official_name"],
        short_name=row["short_name"],
        league=row["league"],
        age_group=row["age_group"],
        home_venue=row["home_venue_name"],
        aliases=aliases,
        alarm_game_min=row["alarm_game_min"],
        alarm_practice_min=row["alarm_practice_min"],
    )


def list_sources(conn: sqlite3.Connection, *, enabled_only: bool = True) -> list[Source]:
    sql = "SELECT * FROM sources"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return [
        Source(
            id=r["id"],
            activity_id=r["activity_id"],
            kind=r["kind"],
            shape=r["shape"],
            tier=r["tier"],
            trust_rank=r["trust_rank"],
            poll_interval_s=r["poll_interval_s"],
            url_template=r["url_template"],
            secret_ref=r["secret_ref"],
            config=json.loads(r["config"] or "{}"),
            enabled=bool(r["enabled"]),
            staging_collection=r["staging_collection"],
        )
        for r in conn.execute(sql)
    ]


def known_hashes(
    conn: sqlite3.Connection, source_id: str, *, since: str | None = None
) -> dict[str, str]:
    """``{uid: content_hash}`` for a source — the left side of a poll diff.

    ``since`` must be the sync window's lower bound, and leaving it out is a bug
    waiting to happen. The incoming side of the diff is window-filtered, so if
    this side is not, every event that ages past the window looks like it
    vanished — and a whole season ageing out at once reads as a mass
    cancellation. Compare like with like.
    """
    sql = ("SELECT uid, content_hash FROM event_state "
           "WHERE source_id = ? AND cancelled = 0")
    params: tuple = (source_id,)
    if since is not None:
        sql += " AND starts_at >= ?"
        params = (source_id, since)
    return {r["uid"]: r["content_hash"] for r in conn.execute(sql, params)}


@dataclass(frozen=True)
class EventState:
    """What we last wrote for one event, and where we wrote it.

    Deliberately not a ``TargetRef``: the state layer records identifiers, and
    stays ignorant of how any given target mints them.
    """

    uid: str
    source_id: str
    collection: str
    remote_id: str | None
    content_hash: str
    remote_etag: str | None
    starts_at: str
    cancelled: bool


def event_states(conn: sqlite3.Connection, source_id: str) -> dict[str, EventState]:
    """Everything we have written for a source, cancelled rows included.

    Cancelled rows are kept so a resurrected event is recognised as the same
    event rather than written a second time.
    """
    return {
        r["uid"]: EventState(
            uid=r["uid"],
            source_id=r["source_id"],
            collection=r["collection"],
            remote_id=r["remote_id"],
            content_hash=r["content_hash"],
            remote_etag=r["remote_etag"],
            starts_at=r["starts_at"],
            cancelled=bool(r["cancelled"]),
        )
        for r in conn.execute("SELECT * FROM event_state WHERE source_id = ?", (source_id,))
    }


def record_event_state(
    conn: sqlite3.Connection,
    *,
    uid: str,
    source_id: str,
    collection: str,
    remote_id: str | None,
    content_hash: str,
    remote_etag: str | None,
    starts_at: str,
) -> None:
    """Record a successful write. Call this only *after* the target accepted it.

    Writing state first would make a failed target write look synced, and the
    event would never be retried.

    ``cancelled`` resets to 0: an event that comes back after being cancelled
    upstream is live again, and its row has to say so or the next diff will
    treat it as new.
    """
    conn.execute(
        """
        INSERT INTO event_state
            (uid, source_id, collection, remote_id, content_hash, remote_etag,
             starts_at, cancelled, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
        ON CONFLICT(uid) DO UPDATE SET
            source_id    = excluded.source_id,
            collection   = excluded.collection,
            remote_id    = excluded.remote_id,
            content_hash = excluded.content_hash,
            remote_etag  = excluded.remote_etag,
            starts_at    = excluded.starts_at,
            cancelled    = 0,
            updated_at   = excluded.updated_at
        """,
        (uid, source_id, collection, remote_id, content_hash, remote_etag, starts_at),
    )


def mark_event_cancelled(conn: sqlite3.Connection, uid: str) -> None:
    """Tombstone a row rather than delete it.

    The row is the only record that this UID was ever ours. Deleting it would
    let the event be adopted from scratch if it reappeared, losing the history
    that says we put it there.
    """
    conn.execute(
        "UPDATE event_state SET cancelled = 1, updated_at = datetime('now') WHERE uid = ?",
        (uid,),
    )


def record_poll_run(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    status: str,
    detail: str | None = None,
    raw_sha256: str | None = None,
) -> int:
    """Log a poll. ``status`` is ok | error | held.

    A held run is the one that most needs a record: the guard tripped, nothing
    was cancelled, and somebody has to go look at why.
    """
    cursor = conn.execute(
        "INSERT INTO poll_runs (source_id, status, detail, raw_sha256) VALUES (?, ?, ?, ?)",
        (source_id, status, detail, raw_sha256),
    )
    return int(cursor.lastrowid or 0)


def record_source_success(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        "UPDATE sources SET last_success_at = datetime('now'), "
        "last_error = NULL, last_error_at = NULL WHERE id = ?",
        (source_id,),
    )


def record_source_error(conn: sqlite3.Connection, source_id: str, error: str) -> None:
    conn.execute(
        "UPDATE sources SET last_error = ?, last_error_at = datetime('now') WHERE id = ?",
        (error, source_id),
    )


def resolve_venue_alias(conn: sqlite3.Connection, alias: str) -> Venue | None:
    """Alias lookup, checked before any geocoder or model call.

    A season has a couple of dozen venues, each resolved once, so the steady
    state costs nothing.
    """
    row = conn.execute(
        """
        SELECT v.* FROM venue_aliases a
          JOIN venues v ON v.id = a.venue_id
         WHERE a.alias = ? COLLATE NOCASE
        """,
        (alias,),
    ).fetchone()
    if row is None:
        return None
    return Venue(
        raw=alias,
        name=row["canonical_name"],
        address=row["address"],
        lat=row["lat"],
        lon=row["lon"],
        pin_confirmed=bool(row["pin_confirmed"]),
    )


def set_staging(conn: sqlite3.Connection, source_id: str, collection: str | None) -> None:
    """Point a source at an onboarding collection, or clear it to promote.

    Clearing is all promotion takes: the next sync computes a different
    collection, and a changed collection is a move rather than an update, so the
    events relocate without duplicating (docs/ONBOARDING.md §4).
    """
    conn.execute(
        "UPDATE sources SET staging_collection = ? WHERE id = ?", (collection, source_id)
    )
    conn.commit()


def get_source(conn: sqlite3.Connection, source_id: str) -> Source | None:
    for source in list_sources(conn, enabled_only=False):
        if source.id == source_id:
            return source
    return None


def set_enabled(conn: sqlite3.Connection, source_id: str, enabled: bool) -> None:
    conn.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))
    conn.commit()
