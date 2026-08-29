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
    official_name   TEXT,                 -- league designation, e.g. U10PL
    short_name      TEXT,
    league          TEXT,
    age_group       TEXT,
    home_venue_id   INTEGER REFERENCES venues(id) ON DELETE SET NULL,
    tz              TEXT NOT NULL,
    season_start    TEXT,
    season_end      TEXT,
    alarm_game_min      INTEGER NOT NULL DEFAULT 90,
    alarm_practice_min  INTEGER NOT NULL DEFAULT 30,
    -- Minutes before kick-off the team expects you at the ground, 0 for a team
    -- that does not ask. Drives the synthetic warm-up event (`warmup.py`).
    warmup_minutes      INTEGER NOT NULL DEFAULT 0,
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
    -- While set, every event from this source routes to this one collection
    -- instead of the normal template: the onboarding calendar
    -- (docs/ONBOARDING.md). Clearing it promotes the source, and the next
    -- sync moves the events because a collection change is a move.
    staging_collection TEXT,
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

-- What the event *was*, as opposed to where we put it.
--
-- Three things about this table are load-bearing, and undoing any of them
-- reintroduces a failure the design exists to avoid:
--
-- 1. **These columns are what the SOURCE said, not what is on the calendar.**
--    The distinction is invisible today, because the feed is the only thing
--    that contributes to an event. It stops being invisible the moment
--    amendments land (docs/MATRIX.md §4): the calendar will then be this layer
--    plus a higher-trust overlay, and a poll must be able to rewrite this layer
--    freely *without* reverting the overlay. That only works because a poll
--    never writes anything but this. Storing rendered values here instead would
--    make every poll fight every amendment, which is the exact silent revert
--    §4 is about.
--
-- 2. **There is no summary column, and there never will be.** The display title
--    is composed from these fields at write time and re-composed at read time,
--    so a naming-convention change re-renders everything without re-fetching.
--    A stored title is a fourth copy that goes stale on a config edit.
--
-- 3. **No coordinates.** Events carry LOCATION as "name, address" and nothing
--    else. `venues` already holds pins and is the only place that should; this
--    table must not become a second one.
--
-- Rows are written in the same call that records placement — after the target
-- accepted the write — so this can never disagree with the calendar. It is a
-- receipt, not a cache. Rows age out with the sync window (`prune_event_content`)
-- because this is children's names, venues and times at rest, and the smallest
-- honest retention is the one the calendar itself keeps.
CREATE TABLE IF NOT EXISTS event_content (
    uid             TEXT PRIMARY KEY REFERENCES event_state(uid) ON DELETE CASCADE,
    ends_at         TEXT NOT NULL,
    tz              TEXT NOT NULL,
    is_game         INTEGER NOT NULL,
    -- A date with no time. `starts_at` on event_state is still an instant
    -- (local midnight), so only the render differs.
    all_day         INTEGER NOT NULL DEFAULT 0,
    opponent        TEXT,
    -- Tri-state on purpose: NULL is "not known", which is different from "home".
    -- Some feeds phrase every fixture as "vs", so away is only ever marked when
    -- positively known, and two states would force a guess.
    home            INTEGER,
    detail          TEXT,
    body            TEXT,
    url             TEXT,
    kit             TEXT,
    arrive_at       TEXT,
    source_category TEXT,
    -- The game this warm-up sits in front of, NULL for everything a feed
    -- actually published. Structural rather than rendered, so it belongs here:
    -- without it nothing reading this table back can tell a warm-up from a
    -- practice, and it would be titled as one.
    warmup_for      TEXT,

    venue_raw       TEXT,
    venue_name      TEXT,
    venue_address   TEXT,
    venue_field     TEXT,
    -- The poll this came from. What tells a reader how stale the answer is.
    observed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL,            -- ok | error | held
    detail      TEXT,
    raw_sha256  TEXT
);

-- Questions calsync asked, and the answers waiting on a human.
--
-- One table rather than "tasks" plus "answers", because it is one lifecycle:
-- a question is asked, something answers it, somebody decides. Splitting it
-- would mean a join to discover a state that is a single column.
--
-- **Nothing here is ever applied on arrival.** An answer sits in `answered`
-- until a person approves it, and approving is what writes the alias, the
-- vocabulary word or the venue row. That is the whole review gate: an agent
-- cannot approve its own answer because approving does not happen here at all,
-- it happens in the console (docs/API.md, "Configuration is not in this API").
--
-- Rows are written when a task is *dispatched*, which is what lets the endpoint
-- refuse an answer to a question calsync never asked. Without that, anything
-- holding the API token could invent a task id and have its answer queued for
-- approval — one bad paste away from a plausible-looking alias in front of a
-- tired human at 11pm.
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    -- The diagnostic behind it, and the docs/MATRIX.md §2 task vocabulary.
    kind          TEXT NOT NULL,
    type          TEXT NOT NULL,
    context       TEXT NOT NULL,              -- JSON array, as the coach typed it
    candidates    TEXT NOT NULL DEFAULT '[]', -- JSON array, best first
    dispatched_at TEXT NOT NULL,

    -- Filled in by whatever answers. Never acted on directly.
    answer        TEXT,                       -- JSON object, shape per `type`
    rationale     TEXT,
    answered_by   TEXT,
    answered_at   TEXT,

    -- open      dispatched, nothing has answered
    -- answered  an answer is waiting for a human
    -- approved  applied; the next poll re-renders and releases the events
    -- rejected  discarded, with the question still open
    -- resolved  the question stopped being asked (somebody answered it by hand)
    state         TEXT NOT NULL DEFAULT 'open',
    decided_at    TEXT
);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS tasks_source ON tasks(source_id);
