"""Load domain objects out of the database.

The normalizers and adapters take plain dataclasses and don't know the DB
exists, which is what keeps them unit-testable without fixtures on disk. This
module is the only thing that bridges the two.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import Activity, Child, Event, Venue


class NotFound(KeyError):
    """A row that was asked for by id does not exist.

    A subclass of KeyError so existing callers that catch KeyError still work,
    but distinct so the web layer can tell "you asked for something that isn't
    there" from a stray dict lookup in our own code. Catching bare KeyError up
    there reported real bugs as ordinary user error, which is the worst place
    for a defect to hide.
    """


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
        raise NotFound(f"no child {child_id!r}")
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
        raise NotFound(f"no activity {activity_id!r}")

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


# --- what the event was, as opposed to where we put it ----------------------
#
# `event_state` answers "did this change, and where did it go". It cannot answer
# "what is on at 5pm on Thursday", which is why `digest.py` re-parses every feed
# and why docs/API.md's read endpoints could not be written. These functions are
# the second half: the content of each event as its *source* reported it.
#
# The ordering rule from `sync.py` extends here unchanged — content is recorded
# in the same call that records placement, after the target accepted the write.
# That is what makes this a receipt rather than a cache, and it is why the API
# and the calendar cannot disagree.


#: Columns that describe the event, in the order they are read and written.
#: `observed_at` is deliberately excluded: it says when we last looked, not what
#: we saw, and including it would make every poll look like a change.
CONTENT_COLUMNS: tuple[str, ...] = (
    "ends_at", "tz", "is_game", "opponent", "home", "detail", "body", "url",
    "kit", "arrive_at", "source_category",
    "venue_raw", "venue_name", "venue_address", "venue_field",
)


def content_of(event) -> dict:
    """The stored form of an ``Event``, ready to compare or write.

    Note what is absent. There is no title, because the title is a render
    composed from these fields (`normalize/title.py`) — storing it would undo
    the thing that lets a naming convention change without re-fetching a feed.
    There are no coordinates, because no coordinates are emitted at all and
    `venues` is the only table that should hold a pin.

    ``home`` stays tri-state through the round trip. Collapsing NULL to 0 would
    turn "we do not know which side is at home" into a claim that it is a home
    fixture, and some feeds phrase every fixture as "vs".
    """
    venue = event.venue
    return {
        "ends_at": event.ends_at.isoformat(),
        "tz": event.tz,
        "is_game": int(event.is_game),
        "opponent": event.opponent,
        "home": None if event.home is None else int(event.home),
        "detail": event.detail,
        "body": event.body,
        "url": event.url,
        "kit": event.kit,
        "arrive_at": event.arrive_at.isoformat() if event.arrive_at else None,
        "source_category": event.source_category,
        "venue_raw": venue.raw if venue else None,
        "venue_name": venue.name if venue else None,
        "venue_address": venue.address if venue else None,
        "venue_field": venue.field if venue else None,
    }


def event_contents(conn: sqlite3.Connection, source_id: str) -> dict[str, dict]:
    """``{uid: content}`` for a source, in the same shape :func:`content_of` returns.

    The sync loop compares these two to decide whether a stored event has drifted
    from what the feed now derives to. A uid absent from this mapping — an event
    written before this table existed — therefore reads as a difference and heals
    itself on the next poll.
    """
    rows = conn.execute(
        f"SELECT c.uid, {', '.join('c.' + col for col in CONTENT_COLUMNS)} "
        "FROM event_content c JOIN event_state s ON s.uid = c.uid "
        "WHERE s.source_id = ?",
        (source_id,),
    )
    return {r["uid"]: {col: r[col] for col in CONTENT_COLUMNS} for r in rows}


def record_event_content(
    conn: sqlite3.Connection, *, uid: str, content: dict, observed_at: str
) -> None:
    """Record what was written. Call this only *after* the target accepted it.

    Same rule as :func:`record_event_state`, for the same reason: content
    recorded ahead of a write that then fails is a second copy that disagrees
    with the calendar, which is the one failure mode a stored copy introduces.
    """
    columns = ("uid", *CONTENT_COLUMNS, "observed_at")
    values = (uid, *(content[col] for col in CONTENT_COLUMNS), observed_at)
    updates = ", ".join(f"{col} = excluded.{col}" for col in (*CONTENT_COLUMNS, "observed_at"))
    conn.execute(
        f"INSERT INTO event_content ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))}) "
        f"ON CONFLICT(uid) DO UPDATE SET {updates}",
        values,
    )


def prune_event_content(conn: sqlite3.Connection, *, before: str) -> int:
    """Drop content for events that have aged out of the sync window.

    This is children's names, venues and start times at rest in one more place,
    so it is kept for exactly as long as it is useful and no longer. The window's
    own lower bound is the right boundary and not merely a convenient one: events
    below it are already filtered out of both sides of the diff, so pruning there
    cannot make a live event look like it drifted.

    The ``event_state`` row survives. It is the record that calsync put this
    event on somebody's calendar, it holds no content beyond a uid, and losing it
    would let the event be adopted from scratch if it ever reappeared.
    """
    cursor = conn.execute(
        "DELETE FROM event_content WHERE uid IN ("
        "  SELECT uid FROM event_state WHERE starts_at < ?)",
        (before,),
    )
    return cursor.rowcount


@dataclass(frozen=True)
class StoredEvent:
    """An event reassembled from its receipt, plus where it went.

    ``event`` is a real :class:`~calsync.models.Event`, which is the point: it
    goes back through ``normalize/title.py`` and ``render.py`` unchanged, so a
    reader gets the same title the calendar has rather than an approximation of
    it — and gets it re-composed now, so a naming-convention change shows up
    without anything being re-fetched.
    """

    event: Event
    source_id: str
    activity_id: str
    child_id: str
    collection: str
    cancelled: bool
    observed_at: str


_STORED_SQL = f"""
    SELECT s.uid, s.source_id, s.collection, s.cancelled, s.starts_at,
           s.content_hash, c.observed_at,
           {', '.join('c.' + col for col in CONTENT_COLUMNS)},
           src.activity_id, a.child_id
      FROM event_state s
      JOIN event_content c ON c.uid = s.uid
      JOIN sources src ON src.id = s.source_id
      JOIN activities a ON a.id = src.activity_id
"""


def _stored_event(row: sqlite3.Row) -> StoredEvent:
    venue = None
    if row["venue_raw"]:
        # No coordinates. Events carry LOCATION as name and address, and
        # `venues` is the only table that holds a pin.
        venue = Venue(
            raw=row["venue_raw"],
            name=row["venue_name"],
            address=row["venue_address"],
            field=row["venue_field"],
        )
    return StoredEvent(
        event=Event(
            uid=row["uid"],
            activity_id=row["activity_id"],
            starts_at=datetime.fromisoformat(row["starts_at"]),
            ends_at=datetime.fromisoformat(row["ends_at"]),
            is_game=bool(row["is_game"]),
            tz=row["tz"],
            venue=venue,
            opponent=row["opponent"],
            home=None if row["home"] is None else bool(row["home"]),
            detail=row["detail"],
            body=row["body"],
            url=row["url"],
            source_id=row["source_id"],
            source_category=row["source_category"],
            content_hash=row["content_hash"],
            kit=row["kit"],
            arrive_at=(
                datetime.fromisoformat(row["arrive_at"]) if row["arrive_at"] else None
            ),
        ),
        source_id=row["source_id"],
        activity_id=row["activity_id"],
        child_id=row["child_id"],
        collection=row["collection"],
        cancelled=bool(row["cancelled"]),
        observed_at=row["observed_at"],
    )


def stored_events(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    child_id: str | None = None,
    activity_id: str | None = None,
) -> list[StoredEvent]:
    """Everything written whose start falls in ``[start, end]``.

    Always bounded. An open-ended read of a family's schedule is not something
    this should make easy, and the retention prune means there is nothing to
    find below the sync window anyway.

    Cancelled events are included. They are tombstones rather than purges
    (docs/API.md), and a caller that cannot see the cancellation is a caller
    still holding the old time.
    """
    sql = _STORED_SQL + " WHERE s.starts_at >= ? AND s.starts_at <= ?"
    params: list = [start, end]
    if child_id:
        sql += " AND a.child_id = ?"
        params.append(child_id)
    if activity_id:
        sql += " AND src.activity_id = ?"
        params.append(activity_id)
    sql += " ORDER BY s.starts_at, s.uid"
    return [_stored_event(row) for row in conn.execute(sql, params)]


def stored_event(conn: sqlite3.Connection, uid: str) -> StoredEvent | None:
    """One event by uid, unbounded by date — you already know which one you want."""
    row = conn.execute(_STORED_SQL + " WHERE s.uid = ?", (uid,)).fetchone()
    return _stored_event(row) if row else None


def venue_ref(conn: sqlite3.Connection, *candidates: str | None) -> sqlite3.Row | None:
    """The `venues` row behind a stored venue name, by any of its aliases.

    Resolved at read time rather than stored, so a pin somebody confirms this
    afternoon is visible against events written last month without re-writing
    any of them.
    """
    for candidate in candidates:
        if not candidate:
            continue
        row = conn.execute(
            """
            SELECT v.id, v.canonical_name, v.address, v.pin_confirmed
              FROM venue_aliases a JOIN venues v ON v.id = a.venue_id
             WHERE a.alias = ? COLLATE NOCASE
            """,
            (candidate,),
        ).fetchone()
        if row is not None:
            return row
    return None


#: How many of a source's own poll intervals may pass before what it wrote is
#: treated as possibly out of date. Two, so a single missed poll — a flaky
#: minute of wifi — is not reported as a problem, but a feed that has genuinely
#: stopped answering is.
STALE_AFTER_INTERVALS = 2


@dataclass(frozen=True)
class Freshness:
    """How much to trust what a source last wrote.

    Anything serving stored content has to be able to say how old it is. Both
    readers use this one definition rather than each inventing a threshold,
    because a digest saying "all fine" while the API says "stale" would be its
    own kind of wrong answer.
    """

    source_id: str
    enabled: bool
    last_success_at: datetime | None
    last_error: str | None
    stale: bool


def source_freshness(
    conn: sqlite3.Connection, *, now: datetime
) -> dict[str, Freshness]:
    """Per-source freshness, keyed by id.

    A disabled source is never "stale": it is not being polled on purpose, and
    reporting a deliberate pause as a fault trains people to ignore the signal.
    """
    out: dict[str, Freshness] = {}
    for row in conn.execute(
        "SELECT id, enabled, poll_interval_s, last_success_at, last_error FROM sources"
    ):
        last = parse_db_stamp(row["last_success_at"])
        enabled = bool(row["enabled"])
        overdue = timedelta(
            seconds=STALE_AFTER_INTERVALS * (row["poll_interval_s"] or 1200)
        )
        out[row["id"]] = Freshness(
            source_id=row["id"],
            enabled=enabled,
            last_success_at=last,
            last_error=row["last_error"],
            # `last_error` is only ever the *most recent* poll's error — a later
            # success clears it — so its presence means the last attempt failed.
            stale=enabled
            and (last is None or row["last_error"] is not None or now - last > overdue),
        )
    return out


def parse_db_stamp(value) -> datetime | None:
    """``datetime('now')`` writes "YYYY-MM-DD HH:MM:SS", in UTC and unmarked."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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


# --- reads the web UI needs -------------------------------------------------
#
# Onboarding is CRUD over these tables in the same process rather than over an
# HTTP API (docs/API.md, "Configuration is not in this API"), so the queries it
# needs belong here with every other query. Each one is a single statement and
# holds no transaction open, because the poller is a second writer on the same
# file.


def child_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Children as raw rows, for the editing form.

    ``Child`` deliberately omits ``color``: it is web-UI only and nothing in the
    sync path should be able to read it. The form still has to edit it.
    """
    return list(conn.execute("SELECT * FROM children ORDER BY birth_order, name"))


def list_sports(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, name, emoji, builtin FROM sports ORDER BY builtin, name"
        )
    )


def list_venues(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, canonical_name, address, lat, lon, pin_confirmed "
            "FROM venues ORDER BY canonical_name"
        )
    )


@dataclass(frozen=True)
class VenueDetail:
    """A venue plus everything that points at it.

    ``aliases`` is the load-bearing part. It is what resolves a place with no
    call, no latency and no variance, and it is the reason venue work amortises
    to nothing: teams churn every season, the parks do not.
    """

    id: int
    name: str
    short_name: str | None
    address: str | None
    lat: float | None
    lon: float | None
    pin_confirmed: bool
    geocoder: str | None
    aliases: tuple[str, ...] = ()
    #: Activities naming this as their home ground.
    home_to: tuple[str, ...] = ()

    @property
    def pinned(self) -> bool:
        """Has a usable pin. Not having one is fine — see :mod:`calsync.web`."""
        return self.lat is not None and self.lon is not None

    @property
    def proposed(self) -> bool:
        """Coordinates nothing human has vouched for yet.

        A model may only ever be the last tier of venue resolution, and whatever
        it proposes stays unconfirmed until somebody looks at it.
        """
        return self.pinned and not self.pin_confirmed


def _venue_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> VenueDetail:
    return VenueDetail(
        id=int(row["id"]),
        name=row["canonical_name"],
        short_name=row["short_name"],
        address=row["address"],
        lat=row["lat"],
        lon=row["lon"],
        pin_confirmed=bool(row["pin_confirmed"]),
        geocoder=row["geocoder"],
        aliases=tuple(
            r["alias"]
            for r in conn.execute(
                "SELECT alias FROM venue_aliases WHERE venue_id = ? ORDER BY alias",
                (row["id"],),
            )
        ),
        home_to=tuple(
            r["name"]
            for r in conn.execute(
                "SELECT name FROM activities WHERE home_venue_id = ? ORDER BY name",
                (row["id"],),
            )
        ),
    )


def venues_detailed(conn: sqlite3.Connection) -> list[VenueDetail]:
    return [
        _venue_detail(conn, row)
        for row in conn.execute("SELECT * FROM venues ORDER BY canonical_name")
    ]


def get_venue_detail(conn: sqlite3.Connection, venue_id: int) -> VenueDetail | None:
    row = conn.execute("SELECT * FROM venues WHERE id = ?", (venue_id,)).fetchone()
    return _venue_detail(conn, row) if row else None


def upsert_venue(
    conn: sqlite3.Connection,
    *,
    venue_id: int | None = None,
    name: str,
    short_name: str | None = None,
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    pin_confirmed: bool = False,
    geocoder: str | None = None,
) -> int:
    """Create or rename a venue, keeping its canonical name as an alias.

    Renaming has to go through here rather than ``config.apply``, which keys on
    ``canonical_name`` and would mint a second venue instead. The old name stays
    an alias: it is a string that has genuinely appeared in a feed, and dropping
    it would make every past event unresolvable again.
    """
    name = name.strip()
    if not name:
        raise ValueError("a venue needs a name")

    if venue_id is None:
        cursor = conn.execute(
            "INSERT INTO venues (canonical_name, short_name, address, lat, lon,"
            " pin_confirmed, geocoder) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, short_name, address, lat, lon, int(pin_confirmed), geocoder),
        )
        venue_id = int(cursor.lastrowid)
    else:
        conn.execute(
            "UPDATE venues SET canonical_name = ?, short_name = ?, address = ?,"
            " lat = ?, lon = ?, pin_confirmed = ?, geocoder = ? WHERE id = ?",
            (name, short_name, address, lat, lon, int(pin_confirmed), geocoder, venue_id),
        )

    conn.execute(
        "INSERT OR IGNORE INTO venue_aliases (venue_id, alias, source) VALUES (?, ?, 'ui')",
        (venue_id, name),
    )
    conn.commit()
    return venue_id


def add_venue_alias(conn: sqlite3.Connection, venue_id: int, alias: str) -> None:
    alias = alias.strip()
    if not alias:
        return
    conn.execute(
        "INSERT OR IGNORE INTO venue_aliases (venue_id, alias, source) VALUES (?, ?, 'ui')",
        (venue_id, alias),
    )
    conn.commit()


def remove_venue_alias(conn: sqlite3.Connection, venue_id: int, alias: str) -> None:
    conn.execute(
        "DELETE FROM venue_aliases WHERE venue_id = ? AND alias = ?", (venue_id, alias)
    )
    conn.commit()


def merge_venues(conn: sqlite3.Connection, *, losing_id: int, winning_id: int) -> int:
    """Fold one venue into another. Returns how many aliases moved across.

    Near-duplicates are the normal way this table goes wrong: three coaches type
    "Riverview", "Riverview Farm Park" and "Riverview Farm Park Soccer Fields"
    for one park, and each becomes its own row with its own pin. Merging keeps
    every alias — they are all real strings seen in real feeds — and adds the
    losing name as one more, so events that used it still resolve.

    One transaction: a half-merged venue is worse than either whole.
    """
    if losing_id == winning_id:
        raise ValueError("a venue cannot be merged into itself")

    losing = conn.execute(
        "SELECT canonical_name FROM venues WHERE id = ?", (losing_id,)
    ).fetchone()
    if losing is None:
        raise NotFound(f"no venue {losing_id}")

    aliases = [
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM venue_aliases WHERE venue_id = ?", (losing_id,)
        )
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO venue_aliases (venue_id, alias, source) VALUES (?, ?, 'merge')",
        [(winning_id, alias) for alias in {*aliases, losing["canonical_name"]}],
    )
    conn.execute(
        "UPDATE activities SET home_venue_id = ? WHERE home_venue_id = ?",
        (winning_id, losing_id),
    )
    # Cascades the losing row's aliases; they have already been copied across.
    conn.execute("DELETE FROM venues WHERE id = ?", (losing_id,))
    conn.commit()
    return len(aliases)


def delete_venue(conn: sqlite3.Connection, venue_id: int) -> None:
    """Safe in a way that deleting a child is not.

    Nothing in ``event_state`` references a venue — events carry theirs by value,
    resolved at sync time — so this cannot strand anything on the calendar. The
    events simply re-render without a pin and the place shows up as unresolved
    again on the source page, which is visible and reversible.
    """
    conn.execute("DELETE FROM venues WHERE id = ?", (venue_id,))
    conn.commit()


def list_activities(conn: sqlite3.Connection) -> list[Activity]:
    return [
        get_activity(conn, r["id"])
        for r in conn.execute("SELECT id FROM activities ORDER BY id")
    ]


def source_row(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    """The columns ``Source`` deliberately leaves out — health, not behaviour."""
    return conn.execute(
        "SELECT last_success_at, last_error, last_error_at FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()


def tracked_events(conn: sqlite3.Connection, source_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM event_state WHERE source_id = ? AND cancelled = 0",
            (source_id,),
        ).fetchone()["n"]
    )


def events_in_collection(conn: sqlite3.Connection, source_id: str, collection: str) -> int:
    """How many live events this source has in one collection.

    Read from `event_state` rather than re-derived, because the question is what
    is actually sitting in that calendar right now — a fresh parse answers a
    different question, and the two disagree in exactly the interesting case:
    right after somebody answers, when the feed parses cleanly but the events
    have not been moved yet.
    """
    if not collection:
        return 0
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM event_state "
            "WHERE source_id = ? AND collection = ? AND cancelled = 0",
            (source_id, collection),
        ).fetchone()["n"]
    )


def recent_polls(
    conn: sqlite3.Connection, source_id: str, *, limit: int = 5
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT started_at, status, detail FROM poll_runs WHERE source_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (source_id, limit),
        )
    )


def previous_season(
    conn: sqlite3.Connection, child_id: str, sport_id: str
) -> Activity | None:
    """The last activity this child had in this sport, for clone-forward.

    Rec teams are recreated every season under a new name with a new feed, but
    the timezone, alarm policy and league are the same every time — so a new
    team is two fields rather than a dozen (docs/ONBOARDING.md §8).
    """
    row = conn.execute(
        "SELECT id FROM activities WHERE child_id = ? AND sport_id = ? "
        "ORDER BY season_start DESC, id DESC LIMIT 1",
        (child_id, sport_id),
    ).fetchone()
    return get_activity(conn, row["id"]) if row else None


def update_activity(
    conn: sqlite3.Connection,
    activity_id: str,
    *,
    name: str,
    emoji: str | None,
    official_name: str | None,
    short_name: str | None,
    league: str | None,
    age_group: str | None,
    home_venue_id: int | None,
    alarm_game_min: int,
    alarm_practice_min: int,
) -> None:
    """Edit the fields of a team that change how its events read and parse.

    Four of these are not cosmetic: ``official_name``, ``short_name``, ``league``
    and ``age_group`` all feed :meth:`Activity.known_tokens`, which is what
    decides whether "U10DA TASL Match vs Beach FC" yields an opponent or nothing
    at all. Editing them re-parses the feed on the next poll.

    ``home_venue_id`` is the only thing that can mark a game as away, since some
    feeds phrase every fixture as "vs" regardless.
    """
    if not name.strip():
        raise ValueError("a team needs a name")
    conn.execute(
        """
        UPDATE activities SET name = ?, emoji = ?, official_name = ?,
               short_name = ?, league = ?, age_group = ?, home_venue_id = ?,
               alarm_game_min = ?, alarm_practice_min = ?
         WHERE id = ?
        """,
        (name.strip(), emoji, official_name, short_name, league, age_group,
         home_venue_id, alarm_game_min, alarm_practice_min, activity_id),
    )
    conn.commit()


def teach_event_type(
    conn: sqlite3.Connection, source_id: str, label: str, *, is_game: bool
) -> None:
    """Record that this source calls a game (or a practice) by this name.

    Stored in ``sources.config`` rather than in code because coaches invent
    labels faster than an adapter can enumerate them — "Playoff Game2" and
    "Skills Session" are both real. The adapter's own vocabulary still applies
    to everything unlisted; this only ever adds.
    """
    label = (label or "").strip()
    if not label:
        return
    row = conn.execute("SELECT config FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise NotFound(f"no source {source_id!r}")

    config = json.loads(row["config"] or "{}")
    key = "game_words" if is_game else "practice_words"
    other = "practice_words" if is_game else "game_words"
    words = [w for w in config.get(key, []) if w.casefold() != label.casefold()]
    words.append(label)
    config[key] = words
    # A label cannot be both. Answering again is a correction, not a conflict.
    config[other] = [
        w for w in config.get(other, []) if w.casefold() != label.casefold()
    ]

    conn.execute("UPDATE sources SET config = ? WHERE id = ?",
                 (json.dumps(config), source_id))
    conn.commit()


def add_activity_alias(
    conn: sqlite3.Connection, activity_id: str, alias: str, *, source: str = "ui"
) -> None:
    """Teach the parser another string that means *us*.

    This is the fix for ``unidentified``: the adapter found an "X vs Y" fixture
    and neither side matched a known token, so it named no opponent rather than
    guessing. One alias and every such fixture resolves — including the ones
    already written, because the title is a render and re-renders from stored
    fields on the next poll.
    """
    alias = alias.strip()
    if not alias:
        return
    conn.execute(
        "INSERT OR IGNORE INTO activity_aliases (activity_id, alias, source) "
        "VALUES (?, ?, ?)",
        (activity_id, alias, source),
    )
    conn.commit()


def remove_activity_alias(conn: sqlite3.Connection, activity_id: str, alias: str) -> None:
    conn.execute(
        "DELETE FROM activity_aliases WHERE activity_id = ? AND alias = ?",
        (activity_id, alias),
    )
    conn.commit()


#: What a child or a sport is holding up, so a delete can refuse with a reason
#: rather than a foreign-key error.
@dataclass(frozen=True)
class Usage:
    activities: tuple[str, ...] = ()
    tracked_events: int = 0

    @property
    def in_use(self) -> bool:
        return bool(self.activities)


def _usage(conn: sqlite3.Connection, column: str, value: str) -> Usage:
    names = tuple(
        r["name"]
        for r in conn.execute(
            f"SELECT name FROM activities WHERE {column} = ? ORDER BY name", (value,)
        )
    )
    events = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM event_state
         WHERE cancelled = 0 AND source_id IN (
               SELECT s.id FROM sources s
                 JOIN activities a ON a.id = s.activity_id
                WHERE a.{column} = ?)
        """,
        (value,),
    ).fetchone()["n"]
    return Usage(activities=names, tracked_events=int(events))


def child_usage(conn: sqlite3.Connection, child_id: str) -> Usage:
    return _usage(conn, "child_id", child_id)


def sport_usage(conn: sqlite3.Connection, sport_id: str) -> Usage:
    return _usage(conn, "sport_id", sport_id)


def delete_child(conn: sqlite3.Connection, child_id: str) -> None:
    """Only ever called once :func:`child_usage` says nothing depends on it.

    The check is not paranoia about foreign keys — they would happily succeed.
    ``children`` cascades to ``activities``, which cascades to ``sources``, which
    cascades to ``event_state``: deleting a child with a live team silently
    discards the record of every event calsync has already written, and those
    events stay in the family's calendar with nothing tracking them. There is no
    way back from that except deleting them by hand in a calendar client.
    """
    conn.execute("DELETE FROM children WHERE id = ?", (child_id,))
    conn.commit()


def delete_sport(conn: sqlite3.Connection, sport_id: str) -> None:
    conn.execute("DELETE FROM sports WHERE id = ? AND builtin = 0", (sport_id,))
    conn.commit()


def get_sport(conn: sqlite3.Connection, sport_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sports WHERE id = ?", (sport_id,)).fetchone()


def id_taken(conn: sqlite3.Connection, table: str, candidate: str) -> bool:
    """Is this primary key already in use?

    ``table`` is never operator input — the callers pass a literal — so the
    interpolation is safe, and there is no way to bind an identifier anyway.
    """
    if table not in ("activities", "sources", "children"):
        raise ValueError(f"refusing to probe {table!r}")
    return (
        conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)).fetchone()
        is not None
    )


# --- questions we asked, and answers waiting on a human ---------------------
#
# The review gate, as rows. An answer arrives, sits, and is applied only when
# somebody approves it — so the agent-versus-human boundary docs/API.md argues
# for is a property of the schema rather than a convention anybody has to
# remember.

OPEN, ANSWERED, APPROVED, REJECTED, RESOLVED = (
    "open", "answered", "approved", "rejected", "resolved"
)


@dataclass(frozen=True)
class Task:
    id: str
    source_id: str
    kind: str
    type: str
    context: tuple[str, ...]
    candidates: tuple[str, ...]
    state: str
    answer: dict | None = None
    rationale: str | None = None
    answered_by: str | None = None
    answered_at: str | None = None

    @property
    def waiting(self) -> bool:
        """Has an answer nobody has decided on yet."""
        return self.state == ANSWERED


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        source_id=row["source_id"],
        kind=row["kind"],
        type=row["type"],
        context=tuple(json.loads(row["context"] or "[]")),
        candidates=tuple(json.loads(row["candidates"] or "[]")),
        state=row["state"],
        answer=json.loads(row["answer"]) if row["answer"] else None,
        rationale=row["rationale"],
        answered_by=row["answered_by"],
        answered_at=row["answered_at"],
    )


def record_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    source_id: str,
    kind: str,
    type: str,
    context: tuple[str, ...],
    candidates: tuple[str, ...],
    dispatched_at: str,
) -> None:
    """Note that this question went out.

    Idempotent on the id, and deliberately does **not** touch an existing row's
    answer or state: task ids are derived from the question, so re-dispatching
    the same unanswered question must not discard an answer that arrived in the
    meantime or un-approve a decision already taken.
    """
    conn.execute(
        """
        INSERT INTO tasks (id, source_id, kind, type, context, candidates,
                           dispatched_at)
             VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            candidates = excluded.candidates,
            dispatched_at = excluded.dispatched_at
        """,
        (task_id, source_id, kind, type, json.dumps(list(context)),
         json.dumps(list(candidates)), dispatched_at),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _task(row) if row else None


def list_tasks(conn: sqlite3.Connection, *, state: str | None = None) -> list[Task]:
    sql = "SELECT * FROM tasks"
    params: tuple = ()
    if state:
        sql += " WHERE state = ?"
        params = (state,)
    sql += " ORDER BY dispatched_at DESC, id"
    return [_task(r) for r in conn.execute(sql, params)]


def record_answer(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    answer: dict,
    rationale: str | None,
    answered_by: str,
    answered_at: str,
) -> None:
    """Store an answer. **Applies nothing.**

    The state moves to `answered`, which is a queue for a human and not an
    instruction to anybody. Whatever wrote this cannot progress it further —
    that is the gate, and it is here rather than in a caller's discipline.
    """
    conn.execute(
        "UPDATE tasks SET answer = ?, rationale = ?, answered_by = ?, "
        "answered_at = ?, state = ?, decided_at = NULL WHERE id = ?",
        (json.dumps(answer), rationale, answered_by, answered_at, ANSWERED, task_id),
    )
    conn.commit()


def decide_task(conn: sqlite3.Connection, task_id: str, state: str) -> None:
    conn.execute(
        "UPDATE tasks SET state = ?, decided_at = datetime('now') WHERE id = ?",
        (state, task_id),
    )


def resolve_stale_tasks(
    conn: sqlite3.Connection, source_id: str, live_ids: tuple[str, ...]
) -> int:
    """Close questions that stopped being asked.

    Somebody answering by hand on the source page makes the diagnostic go away,
    and a task nobody will ever ask again should not sit in the queue implying
    otherwise. Only open and answered rows: a decision already taken is history.
    """
    placeholders = ",".join("?" * len(live_ids))
    sql = (
        "UPDATE tasks SET state = ?, decided_at = datetime('now') "
        "WHERE source_id = ? AND state IN (?, ?)"
    )
    params: list = [RESOLVED, source_id, OPEN, ANSWERED]
    if live_ids:
        sql += f" AND id NOT IN ({placeholders})"
        params.extend(live_ids)
    cursor = conn.execute(sql, params)
    return cursor.rowcount
