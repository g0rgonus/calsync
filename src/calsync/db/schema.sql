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
-- One merge step at a time across the poller, console and inbound processes. A
-- lease rather than a transaction, because the step calls the calendar server.
CREATE TABLE IF NOT EXISTS merge_lease (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    held_until  TEXT NOT NULL DEFAULT '',
    token       TEXT NOT NULL DEFAULT '',     -- which run holds it
    rerun       INTEGER NOT NULL DEFAULT 0    -- a skipped run asked the holder to go again
);
INSERT OR IGNORE INTO merge_lease (id) VALUES (1);

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
    -- A person said this event does not belong on the calendar: NULL normally,
    -- otherwise one of `withheld.REASONS`. It survives every later poll, which
    -- is the whole point — the feed goes on publishing the event forever, and
    -- without a stored decision the next poll would put it straight back.
    -- `cancelled` records that it is off the calendar; this records who
    -- decided and why (`withheld.py`).
    withheld        TEXT,
    -- When a person confirmed this event's removal after a guard held it.
    -- Recorded before the delete is sent, so a reader of the calendar server
    -- never sees an absence calsync cannot account for. Cleared by the next
    -- write: an event that is written again is live again.
    removal_approved_at TEXT,
    -- When the feed marked this event cancelled. Recorded before the delete,
    -- like removal_approved_at, and for the same reader; cleared the same way.
    cancelled_upstream_at TEXT,
    -- When the event is on the calendar, which differs from `starts_at` only
    -- while a person's override moves it. `starts_at` stays the feed's start,
    -- because the diff and the sync window compare like with like; this is the
    -- one everything asking "has it happened yet" or "which day is it on" reads.
    shown_starts_at TEXT,
    -- The overrides the last accepted write carried, as JSON, or NULL. The
    -- receipt for the overlay half of an event: `event_content` holds what the
    -- source said, this holds what a person laid on top, and together they are
    -- what is on the calendar (`overlay.py`).
    applied_overlay TEXT,
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
--    The calendar is this layer plus `event_overrides`, a person's corrections
--    (docs/MATRIX.md §4), and a poll must be able to rewrite this layer
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

-- A person's correction to one event, one row per unit of it (`overlay.py`).
--
-- The overlay half of "calendar content = source layer + overlay"
-- (docs/API.md, "What calsync remembers"). A poll writes `event_content` and
-- never writes here, which is what stops a poll reverting a correction: the
-- two layers have different owners.
--
-- A unit is what a coach changes together — `time` is start, end and all-day
-- as one, `location` is venue and field as one — so a fix to half of a unit
-- can never be mistaken for a fix to the whole. `value` and `baseline` are
-- JSON in `event_content`'s own column representation. `baseline` is what the
-- source said when the override was set: it is what tells "the feed has not
-- moved" from "the feed has moved to something else".
--
-- Against `event_state`, which is tombstoned rather than deleted, so an
-- override outlives its event's cancellation and is still there if the event
-- comes back.
CREATE TABLE IF NOT EXISTS event_overrides (
    uid         TEXT NOT NULL REFERENCES event_state(uid) ON DELETE CASCADE,
    unit        TEXT NOT NULL,
    value       TEXT NOT NULL,
    baseline    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    actor       TEXT,
    -- Incremented on every edit by a person, so a poll that read an older row
    -- can tell, and leaves it alone rather than clearing a value it never saw.
    revision    INTEGER NOT NULL DEFAULT 1,
    set_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- `active`; `changed` once the source says something that is neither the
    -- baseline nor the override, which only a person can resolve; `orphaned`
    -- once the event itself has gone from the source. A poll moves a row
    -- between these and may delete one the source has caught up with. It never
    -- changes `value`.
    status      TEXT NOT NULL DEFAULT 'active',
    -- What the source said when it last disagreed, and when.
    upstream_seen    TEXT,
    upstream_seen_at TEXT,
    PRIMARY KEY (uid, unit)
);

-- Every change to an override, by whoever made it. Append-only; pruned with
-- the sync window like the content it describes, because old and new values
-- are times and places as much as `event_content` is.
CREATE TABLE IF NOT EXISTS override_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    unit        TEXT NOT NULL,
    -- set | edit | remove | kept | auto_clear | changed | settled |
    -- orphaned | returned | reattached | forgot
    action      TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    upstream    TEXT,
    actor       TEXT,
    reason      TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS override_log_uid ON override_log(uid);

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
-- it happens in the console.
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

-- What a tool has pushed to an inbound source (`calsync.inbound`): one row per
-- event the sender named, keyed by its own id. This is the inbound source's
-- equivalent of a feed, and the sync loop reads it where it would otherwise
-- fetch. Rows are never deleted while the source exists — a DELETE from the
-- sender sets `cancelled` — so nothing can leave the store by omission, which
-- is the one way an absence could reach the disappearance guard.
CREATE TABLE IF NOT EXISTS inbound_events (
    source_id    TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id  TEXT NOT NULL,
    state        TEXT NOT NULL,              -- JSON: event, overrides, retired
    cancelled    INTEGER NOT NULL DEFAULT 0,
    etag         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (source_id, external_id)
);

-- Inbound keys, by SHA-256. Revoked, never deleted while their team exists.
-- scope: team (one source) | household_read | admin
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL CHECK (scope IN ('team', 'household_read', 'admin')),
    source_id    TEXT REFERENCES sources(id) ON DELETE CASCADE,
    label        TEXT NOT NULL,       -- who holds it
    sha256       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    last_used_at TEXT,
    revoked_at   TEXT,
    CHECK ((scope = 'team') = (source_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS api_keys_sha256 ON api_keys(sha256);
-- At most one working key per team, and one household read key.
CREATE UNIQUE INDEX IF NOT EXISTS api_keys_one_team
    ON api_keys(source_id) WHERE scope = 'team' AND revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS api_keys_one_household_read
    ON api_keys(scope) WHERE scope = 'household_read' AND revoked_at IS NULL;

-- Append-only. actor: a key's label, or "console".
CREATE TABLE IF NOT EXISTS admin_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL DEFAULT (datetime('now')),
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    subject  TEXT,
    detail   TEXT              -- JSON, never a key and never an event body
);

-- A create's response by (admin key, Idempotency-Key), so a retry gets it back
-- instead of creating twice. Never holds a key. Kept a day.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key_id          INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    idem            TEXT NOT NULL,
    request_sha256  TEXT NOT NULL,
    status          INTEGER NOT NULL,
    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (key_id, idem)
);

-- More kids on one team. `activities.child_id` stays the team's first kid;
-- these are the rest, so one shared team writes one event for all of them.
CREATE TABLE IF NOT EXISTS activity_children (
    activity_id  TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    child_id     TEXT NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    PRIMARY KEY (activity_id, child_id)
);

-- One kid not going to one event of a shared team. The event comes off the
-- calendar only when every kid on it is absent.
CREATE TABLE IF NOT EXISTS event_absences (
    uid       TEXT NOT NULL REFERENCES event_state(uid) ON DELETE CASCADE,
    child_id  TEXT NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    actor     TEXT,
    set_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (uid, child_id)
);

-- One row per game a source has on, keyed so teams going to the same meet land
-- together (`correlate.py`). Replaced whole at every sync of the source, and
-- read by `merging.publish`, which never fetches. `content` is the event after
-- the person's overlay (`repo.content_of` plus its identity), enough to rebuild
-- it; `kids` is the team's kids before absences, which publish reads live.
CREATE TABLE IF NOT EXISTS shared_candidates (
    uid        TEXT PRIMARY KEY,
    source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    kids       TEXT NOT NULL,
    kid_order  INTEGER NOT NULL,
    body       TEXT,
    url        TEXT,
    kit        TEXT,
    arrive_at  TEXT,
    venue      TEXT,
    field      TEXT,
    content    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shared_candidates_key ON shared_candidates(key);

-- Which team copies a merged event stands in for. A copy is taken off the
-- calendar only once its row is here and the merged event is accepted.
CREATE TABLE IF NOT EXISTS merged_members (
    member_uid TEXT PRIMARY KEY,
    merged_uid TEXT NOT NULL,
    source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS merged_members_merged ON merged_members(merged_uid);
