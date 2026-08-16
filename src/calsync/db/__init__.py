"""SQLite connection, migration and seed data.

Stdlib sqlite3 on purpose: this runs on one box for one household, and an ORM
would be more dependency than the whole schema is worth.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

#: Additive column migrations, applied when absent. `schema.sql` uses
#: CREATE TABLE IF NOT EXISTS, so it never alters a table that already exists —
#: anything added to an existing table has to be listed here too.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # v2: targets don't all use our UID as their remote identifier.
    ("event_state", "remote_id", "TEXT"),
    # v3: stage one source to an onboarding calendar without moving others.
    ("sources", "staging_collection", "TEXT"),
)

#: Seeded so a fresh install can create an activity without inventing an emoji.
#: `builtin` rows are replaceable — a deployment can override the emoji or add
#: its own sport, and neither is overwritten on later migrations.
BUILTIN_SPORTS: tuple[tuple[str, str, str], ...] = (
    ("soccer", "Soccer", "⚽️"),
    ("basketball", "Basketball", "🏀"),
    ("baseball", "Baseball", "⚾️"),
    ("softball", "Softball", "🥎"),
    ("football", "Football", "🏈"),
    ("swimming", "Swimming", "🏊"),
    ("volleyball", "Volleyball", "🏐"),
    ("tennis", "Tennis", "🎾"),
    ("hockey", "Hockey", "🏒"),
    ("lacrosse", "Lacrosse", "🥍"),
    ("track", "Track & Field", "🏃"),
    ("cross_country", "Cross Country", "🏃"),
    ("gymnastics", "Gymnastics", "🤸"),
    ("dance", "Dance", "🩰"),
    ("martial_arts", "Martial Arts", "🥋"),
    ("golf", "Golf", "⛳️"),
    ("wrestling", "Wrestling", "🤼"),
    ("cycling", "Cycling", "🚴"),
    ("rowing", "Rowing", "🚣"),
    ("skiing", "Skiing", "⛷️"),
    ("climbing", "Climbing", "🧗"),
    ("music", "Music", "🎵"),
    ("theater", "Theater", "🎭"),
    ("scouts", "Scouts", "🏕️"),
    ("chess", "Chess", "♟️"),
    ("robotics", "Robotics", "🤖"),
    ("other", "Other", "📅"),
)

#: Instance defaults. Every one is overridable through the web UI, and none of
#: them encodes a particular household's choices.
DEFAULT_SETTINGS: dict[str, str] = {
    # Where normalized events are written. calsync stops here; syncing onward
    # to iCloud or anywhere else is a separate tool's job.
    "radicale_url": "http://localhost:5232",
    "radicale_user": "calsync",
    "radicale_secret_ref": "radicale_password",
    # Which collection an event lands in. Fields: {type} {child} {sport}
    # {activity}. "{type}" gives games/practices; "{child}" gives one
    # collection per kid; "{child}-{type}" gives both.
    "collection_template": "{type}",
    "collection_game_label": "games",
    "collection_practice_label": "practices",
    # Title rendering. Empty fields collapse, so no dangling separators.
    "title_template": "{kids} {emoji} {detail}",
    "multi_kid_style": "initials",   # initials | names
    "all_kids_label": "Kids",
    "all_kids_threshold": "3",
    "home_marker": "vs",
    "away_marker": "@",
    # Safety. Raising these weakens the protection against a truncated feed
    # being read as a mass cancellation.
    "max_disappearance_pct": "0.20",
    "max_disappearance_count": "3",
    "sync_window_back_days": "7",
    "sync_window_forward_days": "365",
    "default_tz": "UTC",
}


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema and seed defaults. Safe to run repeatedly."""
    conn.executescript(SCHEMA_PATH.read_text())

    for table, column, decl in ADDED_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    conn.executemany(
        "INSERT OR IGNORE INTO sports (id, name, emoji, builtin) VALUES (?, ?, ?, 1)",
        BUILTIN_SPORTS,
    )
    # INSERT OR IGNORE so an operator's edited value is never clobbered by a
    # later migration.
    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        list(DEFAULT_SETTINGS.items()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
    )
    conn.commit()


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
