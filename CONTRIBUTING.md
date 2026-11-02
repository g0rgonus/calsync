# Contributing

Issues and pull requests are welcome. This file is everything you need before
changing calsync: how to run it, what must never break, and how work lands.

## Setup and tests

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                          # the whole suite, offline, ~20s
cd mac && swift test                      # the Mac mirror (macOS only)
```

`pyproject.toml` puts `src` on pytest's path, so tests import the package
directly; the editable install is for the third-party deps and the `calsync`
command.

**The acceptance suite** runs the real sync against a live Radicale and checks
the CalDAV contract a server must meet. It is skipped unless asked for, and CI
runs it on every pull request. A change to a target must pass it:

```bash
scripts/dev-stack.sh
CALSYNC_ACCEPTANCE=1 .venv/bin/pytest tests/test_acceptance.py -v
```

There is no linter or formatter. Please don't add one.

**Test deployment changes on Linux.** The image runs as uid 10001. A Linux bind
mount keeps host ownership, so anything writing to one needs `--user`, and a
`600` file read from one must be owned by 10001. macOS hides all of this.

## Layout

```text
src/calsync/
  sources/      one adapter per feed: bytes in, Events out, plus a content hash
  diff.py       what changed since the last write, and the guards
  render.py     an Event to what a calendar should show
  targets/      write a rendered event: CalDAV, .ics directory, (Google, withdrawn)
  sync.py       the only module that closes the loop
  repo.py       the only module that touches SQLite
  web/          the console
  api/          the read API (GET /v1 is its contract)
  inbound/      the inbound API for pushed sources and admin calls
mac/            calsync-mirror, a separate Swift program (see mac/README.md)
deploy/         assets init-deploy writes: Radicale and Caddy config
tests/fixtures/ recorded feeds, rewritten into the invented world
docs/           events.md, and one doc per connector
```

Adapters and normalizers never touch the database, which is what keeps them
testable without fixtures on disk. Keep it that way.

## Invariants

Each one encodes a failure that wipes, duplicates or misfiles a family's
calendar. They are contracts, not defaults. Each names where it is enforced, so
a reviewer can point at the line a change breaks.

### Calendar safety

1. **calsync writes and removes only events it created.** Ownership is
   `event_state` (`sync.py`); on the Mac it is the `Managed by calsync — uid:`
   line in the notes (`Marker.swift`). A hand-made appointment has no source,
   so nothing can ever justify touching it.
2. **A poll where more than 20% or more than 3 tracked events vanish holds every
   cancellation** until a person confirms that exact set (`diff.py`,
   `Diff.confirm`). A truncated fetch looks exactly like a cancelled season.
   Never raise the thresholds to make a test pass; the console refuses to widen
   them past 0.5 or 25 (`web/app.py:LIMITS`).
3. **Total identity turnover holds creations as well as deletions** (`diff.py`).
   A feed that re-mints every uid would otherwise double the season, and the
   disappearance guard alone only withholds deletions.
4. **A feed's `STATUS:CANCELLED` is obeyed with no guard** and left out of the
   guard's arithmetic on both sides (`diff_poll`, `called_off`). It is a
   statement about one event, not an absence. It is recorded before the delete
   is sent, so the Mac mirror can account for it.
5. **Order is the safety in `sync.py`.** A failed fetch aborts before the diff,
   since zero events and "all cancelled" are the same shape. State is recorded
   only after the target accepts a write, or a failed write would look synced
   and never be retried.
6. **A changed collection is a move, never an update** (`targets.move_required`).
   Collections are separate URLs; an update would leave a copy in the old one.
7. **A finished season is switched off, never erased.** `seasonend.py` stops
   polling a quiet team; `retire.py` cancels only events that have not started.
   Past events are the record of a season the kids played. A source with
   `persists_across_seasons` is never switched off.
8. **A kid with a team cannot be deleted** (`repo.child_usage`). The cascade
   would drop the record of every event written for them and strand those
   events on the calendar.

### Feeds

9. **Change detection uses calsync's own hash over content fields** (each
   adapter's `HASH_FIELDS`). Upstream `SEQUENCE` and `LAST-MODIFIED` mean
   something different in every feed and churn on unchanged events. Never key
   on them and never pass `SEQUENCE` on.
10. **An empty or unparseable feed raises** (`FeedError`). Downstream, zero
    events means everything was cancelled.
11. **Feeds have no format: coaches type them.** Adapters try every known shape,
    take whichever fires, and report the misses instead of guessing. Expect the
    next team to break the pattern.
12. **Datetimes are absolute instants** (`Event.__post_init__` rejects naive
    ones). A naive datetime means an adapter lost the offset.
13. **An all-day event is anchored at local midnight in the team's zone**, never
    UTC, and gets no alarm.
14. **Home or away is never guessed.** Away needs an explicit `@` or a venue
    that isn't the home ground (`normalize/venue.matches_home`); unknown is
    shown as `vs`.

### Rendering

15. **The title is a render, not data** (`normalize/title.py`). `Event` has no
    summary field, nothing stores a title, and nothing parses one back. That is
    what lets every event re-render when a convention changes. See
    [docs/events.md](docs/events.md).
16. **No coordinates are emitted** (`render.py`). Location is "Venue Name, Street
    Address". Radicale truncates `geo:lat,lon` at the comma, which once produced
    confident pins at longitude 0.
17. **Venue identity excludes the field designator** (`normalize/venue.split_field`).
    `Anchorhead #2` is venue `Anchorhead`, field `#2`; folding them mints a venue
    per field.
18. **Renaming or merging a venue keeps the old names as aliases**
    (`repo.upsert_venue`, `repo.merge_venues`). A feed really used that name.
19. **Configuration is data, not code.** Calendar split, title format, multi-kid
    style and the guard thresholds are rows in `settings`.
    `tests/test_configurable.py` configures a different family and fails on
    anything hard-coded.

### People and review

20. **An event calsync cannot place waits in `enrichment`** until someone answers
    on `/review` (`routing.collection_for`). Guessing put 12 of 20 events on the
    wrong calendar once, and correcting that is a move. An unresolved venue does
    *not* hold an event: it costs a map pin, not a calendar
    (`BLOCKING_DIAGNOSTICS`).
21. **The review gate is structural.** `POST /v1/tasks/{id}/result` stores an
    answer and has no parameter that applies it; applying is `enrichment.apply`,
    reached only from the console. An approved answer writes the same rows a
    hand-typed one does.
22. **A model is the last tier of venue resolution, never the first.** Aliases
    answer known venues with no call. Anything a model proposes stays
    `pin_confirmed = 0` until a person confirms it.
23. **Nothing in the poll path sets `withheld`** (`withheld.py`). Taking an event
    off the calendar is a person's decision, made in the console or through the
    admin API. A withheld event is dropped on the way to the write, never out of
    the diff, so it does not count as a disappearance.
24. **Corrections are an overlay** (`overlay.py`). A poll writes the source layer
    only. It may lift a fix the feed has caught up with, and flags one the feed
    has moved away from, but never writes a fix's value
    (`sync._judge_overrides`). That is why a correction survives the next poll.
25. **`event_content` is a receipt** (`repo.record_event_content`): written in the
    same step as placement, after the target accepted the write, holding what the
    *source* said. Every reader (digest, `/calendar`, the read API) reads it, and
    never reads the calendar back or re-parses a feed (`digest.collect`). Rows age
    out with the sync window; the calendar server holds the only copy of past
    seasons.
26. **One event for kids on different teams, and order is the safety**
    (`merging.publish`). A team's own copy comes down only after the merged event
    is accepted and recorded; when a group ends, members' copies are restored
    before the merged event is cancelled. One merge step runs at a time
    (`merge_lease`). The full rules are in `correlate.py` and `merging.py`.
27. **A warm-up is derived from its game** (`warmup.py`) and does not count
    toward the disappearance guard (`counts_as_evidence`). Counting it doubles
    every disappearance in a games-only feed.

### APIs and console

28. **A pushed source's sender describes and never renders** (`inbound/events.py`).
    `title`, `calendar` and `child` are refused by name. Items are never removed
    from the store: a `DELETE` is an explicit cancellation, and a date a series
    stops producing is recorded as cancelled, so the guard never sees an absence.
29. **Every admin change that can reach a live calendar sends a push and can be
    undone through the API** (`alert` in `inbound/app.py`). Keep that true for
    new admin calls.
30. **API contracts are generated, not written.** `GET /v1` (`api/contract.py`)
    and the inbound docs and OpenAPI spec (`inbound/contract.py`) are tested
    against the route tables in both directions. Reserved-but-unbuilt paths
    answer 501 with the reason.
31. **Secrets never live in the database.** A feed URL is split into a template
    plus a secret-store reference, then reassembled and compared before it is
    saved (`onboarding.templatise`).
32. **The console has no login and guards writes with `Sec-Fetch-Site`**, never
    an `Origin`-vs-`Host` comparison, which any host-rewriting proxy turns into
    an outage. The read API is a separate app behind a bearer token. Don't merge
    the two or give either the other's posture.
33. **Console form fields are read through `_field()`** (`web/app.py`). Bottle
    decodes form values as latin-1, and a plain `.get` mangles every emoji and
    accented name.
34. **The inbound service never reads `Authorization`.** That header belongs to
    whatever authenticating proxy is in front, and a 401 says `CalsyncKey`,
    never `Basic`.
35. **Migrations only add, and run concurrently** (`db.migrate`). Every service
    opens the database at start-up, so each statement is idempotent and the
    `ALTER TABLE` checks run under `BEGIN IMMEDIATE`. An older image must still
    start on a newer database.

## Fixtures and the invented world

Every person, team, school, venue, town and address anywhere in this repo is
from the invented world below: tests, fixtures, docs, comments, example output.
Evidence from a live feed is rewritten into that world before it goes in any
file, keeping its shape. **Never commit a feed payload as it arrived.** It is a
location history for real children, and `DESCRIPTION` fields can name other
families' kids.

**Shape is the test, names are not.** Keep punctuation, spacing, casing and word
count exactly: these are what the golden tests check.

- `Theed Park 1009 Theed Rd, Naboo NX 40114`: comma before the city, none
  before the state.
- `Jedi Temple Memorial Park 7160 Senate Ln, Coruscant, NX 40219`: the same,
  properly punctuated.
- `Anchorhead #2` and `Anchorhead#2`: a field designator with and without a
  space.
- `Anchorhead Meadow Park Soccer Fields`: plural with no number, which must not
  split.
- `Anchorhead` / `Anchorhead Meadow Park` / `… Soccer Fields`: three names for
  one place.
- Trailing spaces, doubled separators, `*` suffixes, `vs.`/`v.`/`@` with and
  without a space, all-caps calendar names.

If a golden test fails after you edit a fixture, you changed a shape. Read the
connector's doc before touching the assertion.

### The household: the Skywalkers

| Who | Name | Initial | Notes |
|---|---|---|---|
| Parents | Anakin, Padmé | | calendars like "Skywalker Kids" |
| Kid | Luke | `Lu` | soccer ⚽️, 360Player club team (Jedi) |
| Kid | Leia | `Le` | swim 🏊, pushed team with meets |
| Kid | Rey | `R` | rec teams on TeamReach |
| Extra kids, only when a test needs more | Ahsoka, Poe, Finn, Jyn, Din | `A`, `P`, `F`, `J`, `D` | initials unique within a test |
| Other families on a team | Wedge, Biggs, Jek, Dak, Hobbie, Zev, Wes, Tycho, Porkins, Janson, Hera, Kanan, Sabine, Ezra | | snack rotas and the like |

Luke and Leia share an L, which is why they have two-letter initials.

### The other family: the Solos

`tests/test_configurable.py` exists to prove nothing is hard-coded, so it
differs in every dimension: Ben (`B`), Jaina (`J`) and Allana (`A`); hockey 🏒 in
`America/Chicago`; team Hoth Tauntauns against the Wampas at Echo Base Ice Rink;
all-kids label "The Rebels".

### Places

The fictional state is `NX`.

| Kind | Names |
|---|---|
| Towns | Naboo (home), Coruscant, Tatooine, Mos Espa, Bestine |
| Parks and fields | Theed Park, Jedi Temple Memorial Park, Anchorhead (Meadow Park, Soccer Fields), Mos Eisley Athletic Complex, Hoth Park, Yavin Park |
| Schools and pools | Lars Homestead Elementary School, Tosche Elementary School, Theed Royal Pool, Mandalore Aquatic Center |
| Practice spots | Dagobah, Endor, Jundland |

### Teams

| Kind | Names |
|---|---|
| Our teams | Rogues, Gungans, Ewoks, Jawas, Jedi (Jedi Academy), Rebel ALLIANCE |
| Opponents | Tuskens, Hutts, Krayts, Wookiees, Banthas, Rancors, Mynocks, Sarlaccs, Dewbacks, Porgs, Stormtroopers, Gundarks, Nerfs, Scarif, Mustafar, Jakku, Lothal, Ryloth Juniors, Felucia Town SC |
| Clubs and schools | Mos Eisley FC, Coruscant Athletic, Cloud City FC, FC Kashyyyk, Rimward United, Kamino B, Hoth Classical, Endor Academy, Bespin Academy, Dagobah Prep |
| Leagues and codes | `U10RS`, `RSL`, `ORSL`, `ROGU` |
| Keys and tools | R2-D2, C-3PO |

Need a name that isn't here? Take one from the same world and add it to these
tables in the same change. Product and protocol names (360Player, TeamReach,
Radicale, Matrix, Pushover, iCloud) are not people or places; leave them.

## Adding a connector

**Every connector:**

- ships with `docs/connectors/<name>.md` (using it, then what to know) and a
  line in the README's list;
- keeps its secrets in the secret store, never in the database or a stored URL;
- is covered by `calsync check` and the console's check button.

**A source:**

- golden tests against a fixture rewritten into the invented world, keeping the
  feed's shapes, and one item in its doc per group of golden tests;
- a uid policy declared in `identity.py`: `passthrough` only once uids are known
  to be stable;
- raises on an empty or unparseable feed;
- hashes content fields, never upstream `SEQUENCE` or `LAST-MODIFIED`.

**A destination:**

- touches only events calsync created;
- treats a changed collection as a move;
- declares `Capabilities` rather than assuming them;
- handles all-day events;
- refuses an unmapped collection rather than guessing;
- passes the acceptance suite, if it is CalDAV.

## Conventions

- **Stdlib-first.** Three runtime dependencies: `icalendar`, `pyyaml` and
  `bottle` (one pure-Python module with no dependencies of its own). A new one
  needs a reason that beats that. `sqlite3` directly, no ORM.
- **Deterministic parsing.** No model in the parse path: the same feed always
  renders the same title, and any change traces to a config change.
- **Comments say why, in a line or two.** Not what the code does, and not its
  history.
- **Shipped config carries config.** `docker-compose.yml`, `.env.example` and
  `deploy/` state what is true now, with one factual line where a constraint is
  non-obvious. No "this used to", no accounts of what went wrong.
- **Commit messages** are imperative and explain why, with no conventional-commit
  prefixes: "Hold an unplaceable event instead of filing it under practices".

## Workflow and releases

- Work on a branch and open a pull request against `main`; a draft is fine.
  CI runs on pull requests, not on branch pushes. Pull requests are
  squash-merged, so `main` gets one commit each.
- A first-time contributor's pull request runs CI once a maintainer approves the
  run. It runs the tests and builds the image, and never publishes.
- A maintainer can publish a pull request as `:dev` by approving its
  `publish-dev` job. Only branches in this repo can be published. The run
  summary prints the image's digest, for pinning that exact build until it is
  pruned.
- calsync uses SemVer. A change that should ship bumps `__version__` in
  `src/calsync/__init__.py` and adds its `## [X.Y.Z]` section to `CHANGELOG.md`
  in the same pull request; CI fails a bump without one. `__version__` is the
  only place the version is written, and `tests/test_packaging.py` holds the CLI,
  the API and the console footer to it.
- Merging a bump releases it. CI tags `vX.Y.Z` on `main`, publishes the image,
  and makes the GitHub release from the changelog section.
- Images are `ghcr.io/g0rgonus/calsync`:

  | Tag | Moves when |
  |---|---|
  | `X.Y.Z`, `X.Y`, `X` | a release (`X` from 1.0.0) |
  | `latest` | a release that is the highest version |
  | `main` | every merge to `main`, for testing |
  | `dev` | a maintainer approves a pull request's `publish-dev` |

  There are no per-commit tags. A weekly job prunes untagged images older than
  30 days, keeping everything a tagged image uses. Only release tags are durable
  pins: a `:main` or `:dev` digest stops resolving about 30 days after the tag
  moves on.

  Release images carry a build provenance attestation:
  `gh attestation verify oci://ghcr.io/g0rgonus/calsync:X.Y.Z -R g0rgonus/calsync`.
- The Mac mirror is versioned separately, in
  `mac/Sources/CalsyncMirrorCore/Version.swift`.
