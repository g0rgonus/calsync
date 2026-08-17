# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

The sync loop runs end to end for two adapters (Player360, TeamReach) into
`ics_file` or CalDAV, with `calsync poll` as a long-running loop and a Docker
Compose stack running it against Radicale. Verified against live feeds and a live
Radicale, including the R1–R8 acceptance checks in
`docs/deployment/radicale.md`.

The onboarding console is built (`calsync web`, `src/calsync/web/`). Paste a
feed URL, confirm three things, and the source is staged; the gate in
`docs/ONBOARDING.md` §5 is the app's primary screen. `/venues` manages the alias
table, pins and merges; `/household` edits kids and the sport catalog;
`/settings` covers the `settings` table. Activity fields are still hand-edited
in SQLite by design.

**There is still no HTTP API.** `docs/API.md`, `docs/MATRIX.md` and
`docs/MATCHING.md` specify components that do not exist — read them as the design
contract, not as a description of the code.

**Configuration does not go through the API.** The console edits children,
activities, sources, venues and settings directly in SQLite via `repo.py` and
`config.py`, in the same process. Reasoning in `docs/API.md`, "Configuration is
not in this API".

The boundary that decision respects is **agent versus human, not network
position**: Hermes and the pollers still submit proposals, still cannot approve
them, and nothing but calsync writes to Radicale. "It's on a private tailnet" is
not a reason to weaken those — the risk there is an agent acting on a bad parse,
not a stranger on the internet.

The Google target is implemented and tested but not wired to the CLI.

## Setup and tests

No virtualenv is committed and the system Python lacks the three runtime deps,
so a fresh clone needs:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                    # 210 tests, ~0.7s
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
calsync --db drive.db web                      # the console, on localhost:8730
```

The console is the same code paths as the CLI with a browser in front. It runs a
live dry-run to render the gate, exactly as `calsync promote` does, rather than
trusting a stored verdict.

To exercise it without a real team's feed, `docker compose --profile demo up -d
web feeds` adds a server replaying the recorded fixtures with their dates
shifted onto this week — without the shift every event falls outside the sync
window and every gate condition passes vacuously.

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

inspection.py  →  onboarding.py  →  config.apply       web/ is a browser over
(bytes to        (draft to rows,     (rows)            those three, plus
 derivations)     credential out)                      sync_source(dry_run=True)
```

The onboarding half runs *before* a source exists, which is why it is separate:
`inspection.py` takes bytes and returns derivations with no database at all, and
`onboarding.py` is the only thing that turns a confirmed inspection into rows.

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
  iCalendar has arbitrary `X-` properties,
  Google has `extendedProperties.private` and constrained base32hex event ids.
  If `render()` returned iCalendar text, every non-CalDAV target would parse it back.
- **Targets declare `Capabilities` rather than assume them**, so the writer
  degrades knowingly instead of emitting properties a target silently drops.
- **A changed collection is a move, not an update.** CalDAV collections are
  distinct URLs, so reclassification is delete-then-create (`targets.move_required`).
  Treating it as an update leaves a ghost copy — the duplicate-in-a-shared-calendar
  failure this project exists to prevent.
- **Deleting a child cascades to `event_state`.** `children` → `activities` →
  `sources` → `event_state`, all `ON DELETE CASCADE`. Removing a kid who still has
  a team discards the record of every event calsync has written, and those events
  stay in the family's calendar with nothing tracking them — the duplicate-in-a-
  shared-calendar failure, arrived at from the other direction. `repo.child_usage`
  exists so a delete refuses with a reason instead of succeeding quietly.
- **Bottle decodes form values as latin-1.** That is the HTTP default when a
  browser omits the charset, which browsers always do. `web/app.py` reads every
  field through `_field()`, which calls `getunicode`; a plain `.get` mangles every
  emoji and every accented name.
- **A feed URL is a credential, so onboarding splits it before storing it.**
  `onboarding.templatise` moves the token to the secret store and leaves
  `{{secret:ref}}` behind, then *reassembles the template and compares it against
  the original*. A URL that will not round-trip is refused rather than saved —
  silently mangling one surfaces weeks later as a source that stopped working,
  with nothing to point at.
- **A finished season does not fail — it keeps serving.** A rec team's app goes
  on returning last spring's fixtures with a clean 200 indefinitely, so looking
  for fetch errors finds a *broken* feed and never finds an ended one. The tell
  is the dates: nothing upcoming, and the newest event months in the past
  (`dormancy.py`). `polling.py`'s backoff is for feeds that stopped answering,
  which is a different and rarer problem — do not conflate them.
- **A finished season is switched off, never erased.** `seasonend.py` nudges at
  a month past the last event and stops polling at two. It does not cancel
  anything: by then *every* event of that season is in the past, so removing
  them would delete the record of a season the kids played. Retiring is a
  separate, manual decision. A source with `persists_across_seasons` in its
  config is never switched off — a club team kept across years goes quiet each
  summer, and disabling it in July means noticing in September.
- **Matrix is outbound only.** `matrix.py` + `digest.py` let calsync talk to a
  room; nothing reads from it, and `docs/MATRIX.md` §7 records exactly which
  arrow is built and what the inbound half is blocked on (no API, no identity
  model, no blast-radius policy). Do not let the settings page or the docs imply
  proposals or approvals exist.
- **A digest re-derives, it does not read back.** The calendar holds renders and
  `event_state` holds no content, so anything that needs event *data* parses the
  feeds again — the same refusal `docs/API.md` gives Hermes. `digest.collect`
  writes nothing at all, and a test diffs the database file to keep it that way.
- **The guard thresholds are bounded in the UI.** `web/app.py:LIMITS` refuses to
  widen `max_disappearance_pct` past 0.5 or the count past 25. Narrowing is free.
  A guard that a web form can switch off in two clicks is not a guard, and the
  invariant above says never raise these to make something pass.
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
- **No coordinates are emitted at all.** Events carry `LOCATION` as
  "Venue Name, Street Address" and nothing else — that is what a maps app needs
  to give a tappable, correct destination. calsync used to also write `GEO` and
  `X-APPLE-STRUCTURED-LOCATION` for an exact pin; it depended on a coordinate
  round-trip that Radicale silently truncated at the comma in `geo:lat,lon`,
  producing a confident pin at longitude 0 instead of a working address. Do not
  reintroduce it. `venues.lat/lon` still exist and are import-only.
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
  field. The console refuses a typed name that carries one, which is the only
  place a human could reintroduce it.
- **Renaming a venue keeps the old name as an alias** (`repo.upsert_venue`).
  The old string is one a feed genuinely used, and dropping it makes every past
  event at that place unresolvable again. Merging (`repo.merge_venues`) keeps
  both sides' aliases for the same reason.
- **Deleting a venue is safe; deleting a child is not.** No `event_state` row
  references a venue — events carry theirs by value, resolved at sync time — so
  a deleted venue costs a pin and reappears in diagnostics. Do not generalise
  that to the other delete paths.
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
  dependency against that. Three runtime deps total; `bottle` is there because it
  is a single pure-Python module with no dependencies of its own, where Flask is
  five packages and the stdlib would leave HTML escaping to be hand-rolled.
- The console has no login. It is loopback-only with one operator, reached
  through whatever VPN or proxy already fronts the homelab — a password form in
  front of a page unreachable without one is a thing to maintain, not a control.
  Writes *are* checked, via `Sec-Fetch-Site`; never reintroduce an `Origin`-vs-
  `Host` comparison, which any Host-rewriting proxy turns into a total outage.
- Normalization is deterministic — no model in the parse path, so the same feed
  always renders the same title and any change traces to a config change.
- Commit messages are imperative and explain the *why*, with no conventional-commit
  prefixes: "Move configuration into the database so other households can deploy this".
- Work happens on branches; `main` is the PR target.
