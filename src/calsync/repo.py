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
