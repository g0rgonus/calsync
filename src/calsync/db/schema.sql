-- calsync schema.
--
-- Everything a deployment needs to describe itself lives here, so no family is
-- baked into the code. The web UI is CRUD over these tables; YAML is only ever
-- an import/export format.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY
);

-- Instance settings: radicale_url, collection_template, title_template, ...
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Prepopulated catalog, extensible per deployment. Nobody should have to pick
-- an emoji for soccer, but somebody will need fencing.
CREATE TABLE IF NOT EXISTS sports (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    emoji       TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS children (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    initial     TEXT NOT NULL,
    birth_order INTEGER NOT NULL DEFAULT 0,
    color       TEXT,
    nicknames   TEXT NOT NULL DEFAULT '[]'   -- JSON array
);
CREATE UNIQUE INDEX IF NOT EXISTS children_initial ON children(initial);

CREATE TABLE IF NOT EXISTS venues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT NOT NULL,
    short_name      TEXT,
    address         TEXT,
    lat             REAL,
    lon             REAL,
    pin_confirmed   INTEGER NOT NULL DEFAULT 0,
    geocoder        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS venues_canonical ON venues(canonical_name);

-- Raw strings seen in the wild that resolve to a venue. Checked before any
-- geocoder or model call, so steady state costs nothing.
CREATE TABLE IF NOT EXISTS venue_aliases (
    venue_id    INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    source      TEXT,
    PRIMARY KEY (venue_id, alias)
);
CREATE INDEX IF NOT EXISTS venue_aliases_alias ON venue_aliases(alias);

CREATE TABLE IF NOT EXISTS activities (
    id              TEXT PRIMARY KEY,
    child_id        TEXT NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,        -- human-readable, appears in titles
    sport_id        TEXT NOT NULL REFERENCES sports(id),
    emoji           TEXT,                 -- overrides the sport's default
    official_name   TEXT,                 -- league designation, e.g. U10DA
    short_name      TEXT,
    league          TEXT,
    age_group       TEXT,
    home_venue_id   INTEGER REFERENCES venues(id) ON DELETE SET NULL,
    tz              TEXT NOT NULL,
    season_start    TEXT,
    season_end      TEXT,
    alarm_game_min      INTEGER NOT NULL DEFAULT 90,
    alarm_practice_min  INTEGER NOT NULL DEFAULT 30,
    enabled         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS activities_child ON activities(child_id);

-- Official names are exactly the strings a coach email or PDF will use, so
-- they belong here rather than being discarded as unfriendly.
CREATE TABLE IF NOT EXISTS activity_aliases (
    activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    source      TEXT,
    PRIMARY KEY (activity_id, alias)
);

CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    activity_id     TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,        -- adapter name, e.g. 'player360'
    shape           TEXT NOT NULL,        -- feed | document | relay
    tier            INTEGER NOT NULL DEFAULT 2,
    trust_rank      INTEGER NOT NULL DEFAULT 2,
    poll_interval_s INTEGER NOT NULL DEFAULT 1200,
    url_template    TEXT,                 -- never the assembled URL
    secret_ref      TEXT,                 -- key into the secret store
    config          TEXT NOT NULL DEFAULT '{}',   -- adapter-specific JSON
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_success_at TEXT,
    last_error      TEXT,
    last_error_at   TEXT
);
CREATE INDEX IF NOT EXISTS sources_activity ON sources(activity_id);

-- What we have already written, and the hash that decides whether to rewrite.
--
-- remote_id is what the *target* calls this event, which is not always our UID:
-- Google requires base32hex ids and derives one, while CalDAV and ics_file use
-- the UID as the resource name. Storing it keeps the state layer from having to
-- know how any particular target mints identifiers.
CREATE TABLE IF NOT EXISTS event_state (
    uid             TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    collection      TEXT NOT NULL,
    remote_id       TEXT,
    content_hash    TEXT NOT NULL,
    remote_etag     TEXT,
    starts_at       TEXT NOT NULL,
    cancelled       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS event_state_source ON event_state(source_id);
CREATE INDEX IF NOT EXISTS event_state_starts ON event_state(starts_at);

CREATE TABLE IF NOT EXISTS poll_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL,            -- ok | error | held
    detail      TEXT,
    raw_sha256  TEXT
);
