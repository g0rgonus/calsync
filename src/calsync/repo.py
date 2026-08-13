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
        )
        for r in conn.execute(sql)
    ]


def known_hashes(conn: sqlite3.Connection, source_id: str) -> dict[str, str]:
    """``{uid: content_hash}`` for a source — the left side of a poll diff."""
    return {
        r["uid"]: r["content_hash"]
        for r in conn.execute(
            "SELECT uid, content_hash FROM event_state "
            "WHERE source_id = ? AND cancelled = 0",
            (source_id,),
        )
    }


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
