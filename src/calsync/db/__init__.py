"""SQLite connection, migration and seed data.

Stdlib sqlite3 on purpose: this runs on one box for one household, and an ORM
would be more dependency than the whole schema is worth.
"""

from __future__ import annotations

import os
import sqlite3
import time
import sys
from pathlib import Path

SCHEMA_VERSION = 7
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

#: Additive column migrations, applied when absent. `schema.sql` uses
#: CREATE TABLE IF NOT EXISTS, so it never alters a table that already exists —
#: anything added to an existing table has to be listed here too.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # v2: targets don't all use our UID as their remote identifier.
    ("event_state", "remote_id", "TEXT"),
    # v3: stage one source to an onboarding calendar without moving others.
    ("sources", "staging_collection", "TEXT"),
    # v4: which dormancy notification has already gone out, so a finished
    # season is mentioned once rather than at every poll for the rest of time.
    ("sources", "dormancy_notified", "TEXT"),
    # v6: which set of open questions has already been posted to the Matrix
    # room. Separate from `review_notified` on purpose — the two announcements
    # have different audiences, and one flag would mean configuring Matrix after
    # a queue opened silently skipped it.
    ("sources", "review_dispatched", "TEXT"),
    # v6: which set of review questions has already been pushed, so a queue
    # nobody has got to yet is announced once rather than every twenty minutes.
    ("sources", "review_notified", "TEXT"),
    # v5 adds `event_content`, which is a new table rather than new columns, so
    # `schema.sql`'s CREATE TABLE IF NOT EXISTS covers it and nothing belongs
    # here. Existing rows backfill themselves: the sync loop treats missing
    # content as a divergence and re-writes the event once.
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
    # to iCloud or anywhere else is a separate tool's job. `targeting.KINDS` is
    # what this may be set to; being in the target registry is not the same as
    # being offered (`targeting.WITHDRAWN`).
    "target_kind": "caldav",
    # Commented out with the rest of the Google destination
    # (`targeting.WITHDRAWN`), so it is not seeded into new deployments and does
    # not need a control on a settings page that no longer offers the target.
    # An existing row is left alone — migrations never delete — so a deployment
    # that already filled this in still has it when Google comes back.
    #     "google_calendar_map": "{}",
    "radicale_url": "http://localhost:5232",
    "radicale_user": "calsync",
    "radicale_secret_ref": "radicale_password",
    # Which collection an event lands in. Fields: {type} {child} {sport}
    # {activity}. "{type}" gives games/practices; "{child}" gives one
    # collection per kid; "{child}-{type}" gives both.
    "collection_template": "{type}",
    "collection_game_label": "games",
    "collection_practice_label": "practices",
    # Where an event goes when calsync cannot tell which calendar it belongs in.
    # Empty disables the hold entirely and restores the older behaviour, where
    # an unplaceable event silently joined the practices — kept reachable
    # because it is the honest escape hatch if this ever gets in the way.
    "enrichment_collection": "enrichment",
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
    # Matrix. Nothing sends a message yet (docs/MATRIX.md describes a component
    # that does not exist); these are stored and verifiable so that when it
    # lands it starts from a configuration a homeserver has already accepted.
    # The token itself is not here — `matrix_secret_ref` names a secret, the
    # same arrangement as `radicale_secret_ref`.
    "matrix_homeserver": "",
    "matrix_user_id": "",
    "matrix_room_id": "",
    "matrix_secret_ref": "matrix_access_token",
    # The read API's bearer token. Named here, stored in the secret store like
    # every other credential, so a settings row stays safe to read and export.
    # `calsync api` refuses to start until the value exists.
    "api_token_ref": "api_token",
    # Pushover, for the few things that need you rather than merely informing
    # you. Both credentials live in the secret store; these name them.
    "pushover_token_ref": "pushover_token",
    "pushover_user_ref": "pushover_user",
    # Daily digest to Matrix. Empty means never — the poller carries it, so
    # there is no cron to forget about and no second container holding secrets.
    "digest_send_at": "",
    "digest_window_hours": "24",
    # When a quiet feed is presumed to be a season that ended. Rows rather than
    # constants because a league with a longer off-season is a different
    # household's problem, not a different build.
    "season_nudge_days": "30",
    "season_shutoff_days": "60",
}


#: Milliseconds to wait for a write lock before giving up. The poller and the web
#: UI are two processes on one file (docs/API.md, "Configuration is not in this
#: API"), and SQLite's default is to fail *immediately* on contention rather than
#: wait — so without this an ordinary config edit during a poll raises
#: "database is locked". WAL already allows readers to proceed throughout; this
#: only covers writer-against-writer, which is brief and rare here.
BUSY_TIMEOUT_MS = 5000


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


#: Prefix for seeding a setting from the environment on a database's first run.
#: `CALSYNC_SETTING_RADICALE_URL=http://radicale:5232` is the one that matters:
#: the container-correct address cannot be a default (it is wrong on a laptop)
#: and could not previously be expressed anywhere but a command somebody had to
#: remember to run, which is how a stack came up healthy and wrote nothing.
#: Declaring it next to the service name it refers to is where it belongs.
SETTING_ENV_PREFIX = "CALSYNC_SETTING_"


def settings_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Settings named by `CALSYNC_SETTING_*`, validated against the defaults.

    An unknown key raises rather than being ignored. A typo'd environment
    variable that quietly does nothing is the same class of failure this whole
    mechanism exists to remove — and in a container it would leave the operator
    reading a compose file that says the right thing while the database says
    something else.
    """
    env = os.environ if env is None else env
    seeded: dict[str, str] = {}
    for name, value in env.items():
        if not name.startswith(SETTING_ENV_PREFIX):
            continue
        # Empty is unset. Compose passes every variable it declares, whether
        # the operator filled it in or not, so `CALSYNC_SETTING_DEFAULT_TZ=""`
        # is what "I did not configure this" looks like from in here — and
        # seeding it would replace a working default with nothing.
        if not value:
            continue
        key = name[len(SETTING_ENV_PREFIX):].lower()
        if key not in DEFAULT_SETTINGS:
            raise ValueError(
                f"{name} names no setting. Known keys: "
                f"{', '.join(sorted(DEFAULT_SETTINGS))}"
            )
        seeded[key] = value
    return seeded


def migrate(conn: sqlite3.Connection, *, attempts: int = 10) -> None:
    """Create the schema and seed defaults. Safe to run repeatedly.

    Retried on a lock, because `PRAGMA journal_mode = WAL` needs an exclusive
    lock to convert a fresh database and fails outright — not after
    `busy_timeout` — when another connection is already open. Every process
    here opens the database at startup, so on a first `up` several are doing
    this at once. The work below is idempotent, so repeating it converges.
    """
    for attempt in range(attempts):
        try:
            _migrate_once(conn)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _migrate_once(conn: sqlite3.Connection) -> None:
    """One pass. See `migrate` for why it may need more than one.

    Safe *concurrently*, too, which is a stronger claim and the one that
    matters: `web`, `api` and the poller all call this on startup, and on a
    fresh database they can be inside it at the same moment. Every statement
    here is individually idempotent — `CREATE TABLE IF NOT EXISTS`,
    `INSERT OR IGNORE` — except the `ALTER TABLE`s, which are guarded by a
    check on the current columns.

    That guard is check-then-act, so it needs the write lock held across both
    halves. `BEGIN IMMEDIATE` takes it up front; without it two processes both
    read "column absent", both ALTER, and the loser gets "duplicate column
    name". Taking it up front is also what avoids the other failure — upgrading
    a read lock to a write lock deadlocks immediately rather than waiting, so
    `busy_timeout` does not cover it and one side sees "database is locked".
    """
    # Outside the transaction below: `executescript` issues its own COMMIT,
    # which would release the lock we are about to take. Every statement in it
    # is `IF NOT EXISTS`, so concurrent runs converge.
    conn.executescript(SCHEMA_PATH.read_text())

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, column, decl in ADDED_COLUMNS:
            if column not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        conn.executemany(
            "INSERT OR IGNORE INTO sports (id, name, emoji, builtin) VALUES (?, ?, ?, 1)",
            BUILTIN_SPORTS,
        )
        # INSERT OR IGNORE so an operator's edited value is never clobbered by a
        # later migration.
        # Seeds, not overrides: environment values are folded into the defaults and
        # go in under the same INSERT OR IGNORE, so they apply to a fresh database
        # and can never fight a value edited later in the console. That asymmetry
        # is deliberate — a variable that reasserted itself on every restart would
        # make the settings page lie, and the settings page is where a person
        # looks. The cost is that editing the variable afterwards does nothing
        # visible, so say so rather than leaving it to be discovered.
        env_seeds = settings_from_env()
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        for key, value in env_seeds.items():
            if key in stored and stored[key] != value:
                print(
                    f"warning: {SETTING_ENV_PREFIX}{key.upper()} says {value!r} but "
                    f"{key} is already set to {stored[key]!r}; the stored value "
                    f"wins. Change it in the console, or with `calsync set {key} "
                    f"{value}`.",
                    file=sys.stderr, flush=True,
                )
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            list({**DEFAULT_SETTINGS, **env_seeds}.items()),
        )
        # DELETE then INSERT, not INSERT OR REPLACE. `version` is itself the primary
        # key, so "replace" collides with nothing and simply appends — the table was
        # accumulating one row per version ever applied (3, 4, 5, ...) while looking
        # like it held the current one. A bare `SELECT version FROM schema_version`
        # then returns the *oldest*, which is worse than useless in the one moment
        # anybody reads this table: working out how far a database got before a
        # migration went wrong. Existing rows collapse on the next migrate.
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
