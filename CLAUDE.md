# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

The sync loop runs end to end for two adapters (Player360, TeamReach) into
`ics_file` or CalDAV, with `calsync poll` as a long-running loop and a Docker
Compose stack running it against Radicale. Verified against live feeds and a live
Radicale, including the R1–R8 acceptance checks in
`docs/deployment/radicale.md`.

**There is no HTTP API and no web UI.** `docs/API.md`, `docs/MATRIX.md` and
`docs/MATCHING.md` specify components that do not exist — read them as the design
contract, not as a description of the code. `docs/ONBOARDING.md` is the spec for
the web UI, and it is the next thing to build.

**Configuration does not go through the API.** The web UI edits children,
activities, sources, venues and settings directly in SQLite via `repo.py` and
`config.py`, in the same process — so the UI can be built now, without the API
existing. Reasoning in `docs/API.md`, "Configuration is not in this API".

The boundary that decision respects is **agent versus human, not network
position**: Hermes and the pollers still submit proposals, still cannot approve
them, and nothing but calsync writes to Radicale. "It's on a private tailnet" is
not a reason to weaken those — the risk there is an agent acting on a bad parse,
not a stranger on the internet.

The Google target is implemented and tested but not wired to the CLI.

## Setup and tests

No virtualenv is committed and the system Python lacks `icalendar`/`pyyaml`, so a
fresh clone needs:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                    # 149 tests, ~0.4s
.venv/bin/pytest tests/test_player360.py -k content_hash    # single test
```

`pyproject.toml` sets `pythonpath = ["src"]`, so pytest imports the package
without an install — but the third-party deps still have to be there, and the
`calsync` console script needs the editable install.

There is no linter or formatter configured. Don't add one uninvited.

## Running it

```bash
calsync --db drive.db init-db
calsync --db drive.db import config.yaml       # docs/config.example.yaml is the shape
calsync --db drive.db sync --out ./out --dry-run
calsync --db drive.db sync --out ./out
calsync --db drive.db status                   # per-source health + recent polls
```

Staging a new feed to an onboarding calendar, then promoting it once the parse
is clean (`docs/ONBOARDING.md`):

```bash
calsync --db drive.db stage tr-hawks          # routes to the `onboarding` collection
calsync --db drive.db promote tr-hawks        # gated on a clean parse + a seen fixture
```

Docker: `docker compose up -d` runs Radicale plus the poller; one-off commands go
through `docker compose run --rm calsync <cmd>`.

`--from-file` replays a saved payload without a credential (needs `--source`),
and `--now <iso>` pins the clock for reproducible runs. Exit codes are
meaningful: `0` ok, `1` error, `3` a guard held — a scheduler should alert on 3
without treating it as a crash.

Secrets never live in the database. `sources.url_template` stores
`{{secret:p360_token}}`; the value comes from `CALSYNC_SECRET_P360_TOKEN` or
`~/.config/calsync/secrets.json` (which must be `chmod 600`).

## Architecture

One direction, five stages, and each boundary passes plain dataclasses:

```
sources/<feed>.py  →  Event      →  diff.py   →  render.py       →  targets/<kind>.py
(parse + hash)        (models.py)   (guards)     (RenderedEvent)    (serialize + write)
                                                                          ↓
                                    sync.py orchestrates ─────────→  repo.record_*
                                                                    (event_state)
```

`sync.py` is the only module that closes the loop, and its **ordering is the
safety**: a failed fetch aborts before the diff (zero events and "all cancelled"
are the same shape), and state is recorded only *after* the target accepts a
write (the reverse makes a failed write look synced and never retried).

`repo.py` is the only module that bridges SQLite and the domain objects. Adapters
and normalizers never touch the DB, which is what keeps them testable without
fixtures on disk — preserve that separation.

Decisions that span several files and are easy to undo by accident:

- **The title is a render, not data.** `Event` deliberately has no `summary`
  field; the display title is composed at write time from a configurable template
  (`normalize/title.py`). Nothing downstream parses it back, so conventions can
  change and every event re-renders without re-fetching. Don't add a stored title.
- **`render()` returns a domain object, not ICS.** Targets serialize it
  themselves, because the wire formats aren't translations of each other —
  iCalendar has `X-APPLE-STRUCTURED-LOCATION` and arbitrary `X-` properties,
  Google has `extendedProperties.private` and constrained base32hex event ids.
  If `render()` returned iCalendar text, every non-CalDAV target would parse it back.
- **Targets declare `Capabilities` rather than assume them**, so the writer
  degrades knowingly instead of emitting properties a target silently drops.
- **A changed collection is a move, not an update.** CalDAV collections are
  distinct URLs, so reclassification is delete-then-create (`targets.move_required`).
  Treating it as an update leaves a ghost copy — the duplicate-in-a-shared-calendar
  failure this project exists to prevent.
- **Configuration lives in the `settings` table, not in code.** Calendar splitting,
  title format, multi-kid style and safety thresholds are all rows. `tests/test_configurable.py`
  asserts this by configuring a *different* family and expecting their conventions.
  A hardcoded name, emoji, or calendar split will fail it.

## Invariants that will bite

These encode failure modes found in a real feed. Weakening any of them can wipe a
family calendar, so treat them as contracts, not defaults:

- **Absence is the only cancellation signal, so a bad fetch looks like a cancelled
  season.** `diff.py` holds all cancellations when >20% or >3 tracked future events
  vanish in one poll. Never bypass the guard or raise the thresholds to make a test pass.
- **A feed's UID may not be stable, and that failure duplicates rather than deletes.**
  One observed source embeds a generation timestamp in the UID, so every poll mints
  fresh ids for the same events. The disappearance guard does not catch this (it
  withholds deletions; this is all creations), so `diff.py` has a *second* guard for
  total identity turnover that withholds both halves. Per-source UID policy lives in
  `identity.py` — `passthrough` is correct only for feeds verified stable.
- **Upstream `SEQUENCE` means something different in every feed** — a churning unix
  mtime, an honest unix mtime, or a non-monotonic flag that decrements. Never
  propagate it and never key change detection off it; `content_hash` is the authority.
- **An empty or unparseable feed must raise, never return zero events.** Downstream,
  zero events is indistinguishable from everything being cancelled (`FeedError`).
- **Change detection uses our own content hash over content fields only.** Player360
  bumps `SEQUENCE`/`LAST-MODIFIED` 2–3s after each event *ends*; including them
  re-pushes every event the evening it happened. Never propagate upstream `SEQUENCE`.
- **Datetimes are absolute instants.** `Event.__post_init__` rejects naive values —
  a naive datetime means an adapter lost the offset, which silently shifts every render.
- **Never invent coordinates.** An unresolvable location keeps its raw text; a
  non-clickable location beats a wrong pin.
- **Feeds have no format — coaches type them.** Three TeamReach teams on the same
  platform in the same season use three incompatible SUMMARY conventions, and
  differ in which fields they populate at all. Adapters read by *strategy* (try
  each known shape, take whichever fires, report the misses) rather than by
  format. A parser that assumes one convention silently mangles the others.
- **A model is the last tier of venue resolution, never the first.** The
  `venue_aliases` table resolves known venues with no call, no latency and no
  variance; a model is only for the residual, and its answer is written back as
  an alias so each venue is resolved once ever. Anything a model proposes stays
  `pin_confirmed = 0` until a human confirms it.
- **Venue identity excludes the field designator.** "Riverview #2" is venue
  `Riverview` plus field `#2` (`normalize/venue.split_field`). Folding the
  designator into the name mints a separate venue, and a separate geocode, per
  field.
- **Never guess home/away.** Some feeds phrase every fixture as "vs", so away is
  marked only when positively known from the venue.
- **calsync manages sourced events only.** Hand-created family appointments are
  never modified or deleted (`docs/MATCHING.md`).
- **Bodies always state venue-local time**, because clients render in the device's
  timezone — otherwise a travelling parent reads a time that isn't the start time.

## Docs

`docs/` carries the reasoning the code can't. Per-source traps live in
`docs/sources/<source>.md`, and the golden tests encode those findings one-to-one —
**if a golden test fails after a feed-format change, read the source doc before
changing the assertion.** `docs/NAMING.md` covers title and location conventions
(including why multi-kid titles use initials), `docs/deployment/radicale.md` holds
the CalDAV server requirements and acceptance checks.

## Conventions

- Stdlib-first and dependency-light on purpose: `sqlite3` directly rather than an
  ORM, hand-rolled deterministic parsing rather than a model call. Justify any new
  dependency against that.
- Normalization is deterministic — no model in the parse path, so the same feed
  always renders the same title and any change traces to a config change.
- Commit messages are imperative and explain the *why*, with no conventional-commit
  prefixes: "Move configuration into the database so other households can deploy this".
- Work happens on branches; `main` is the PR target.
