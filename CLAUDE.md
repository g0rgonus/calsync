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
`/settings` covers the `settings` table, and a team's own fields are on its
source page. Nothing in the day-to-day path needs sqlite3 any more.

**The HTTP API** (`calsync api`, `src/calsync/api/`) serves `GET /v1/events`,
`GET /v1/events/{uid}` and `POST /v1/tasks/{id}/result`, behind a bearer token
from the secret store. The write endpoint stores an answer and **applies
nothing** — approving happens in the console, by a person. It rests on `event_content`, which records what each event was
alongside `event_state`'s record of where it went (`docs/API.md`, "What calsync
remembers"). The write half — documents, proposals, approvals, task tokens,
`PATCH` and its amendment overlay — is deliberately unbuilt, because none of its
consumers exist and a review gate with nothing to review cannot be shown to work.

Events calsync **cannot place** are held in an `enrichment` collection instead
of being filed under a guess, and `/review` is where a human answers the
question that releases them. That is the human half of the Hermes design
(`docs/MATRIX.md`) and it works with no agent involved; Hermes becomes a second
answerer of the same three questions later.

`docs/MATCHING.md` and everything in `docs/API.md` past the
two read endpoints specify components that do not exist — read those as the
design contract, not as a description of the code.

**Configuration does not go through the API.** The console edits children,
activities, sources, venues and settings directly in SQLite via `repo.py` and
`config.py`, in the same process. Reasoning in `docs/API.md`, "Configuration is
not in this API".

The boundary that decision respects is **agent versus human, not network
position**: Hermes and the pollers still submit proposals, still cannot approve
them, and nothing but calsync writes to Radicale. "It's on a private tailnet" is
not a reason to weaken those — the risk there is an agent acting on a bad parse,
not a stranger on the internet.

The Google target is **withdrawn as a destination** — not in `targeting.KINDS`,
not in the `--target` choices, not in the console's picker. Its payload builder
is complete and tested and stays registered in `targets/`; only the OAuth
exchange is missing, tracked at
https://github.com/g0rgonus/calsync/issues/1. It was previously offered with a
caption explaining that it could not work, which was the wrong call: an entry in
a dropdown reads as a supported choice however the caption is worded. Selecting
it anyway — an old `target_kind` row, say — gives the reason and the issue link
rather than "unknown target kind" (`targeting.WITHDRAWN`).

The daily digest goes out from the poll loop itself (`cli.py:_maybe_digest`),
gated on the `digest_send_at` setting, which is empty by default and means
never. There is no cron entry and no second container — the poller already has
the database and the secret store to hand.

## Setup and tests

No virtualenv is committed and the system Python lacks the three runtime deps,
so a fresh clone needs:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                    # 467 tests, ~2.7s
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
calsync --db drive.db api                      # the read API, on localhost:8731
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

Docker: `docker compose up -d` runs Radicale, the poller and the console;
one-off commands go through `docker compose run --rm calsync <cmd>`. The read
API is opt-in — `docker compose --profile api up -d api` — because its only
intended consumer does not exist yet and an authenticated listener nothing talks
to is surface without a purpose.

Backups: `scripts/backup.sh [DEST]`, daily from cron on the host. It takes a
live-safe SQLite snapshot, tars Radicale's data and the credentials, verifies
what it produced and exits non-zero if anything is missing. Each backup carries
its own `RESTORE.md`.

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
                                                        (event_state + event_content)
                                                                          ↓
inspection.py  →  onboarding.py  →  config.apply                     api/ reads it
(bytes to        (draft to rows,     (rows)                          back out as
 derivations)     credential out)                                    fields

web/ is a browser over the onboarding row, plus sync_source(dry_run=True).
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
- **A finished season is switched off, never erased — and that applies to
  retiring too.** `seasonend.py` nudges at a month past the last event and stops
  polling at two, cancelling nothing. `retire.py` cancels **only events that
  have not started yet**, so retiring a finished season removes nothing and just
  stops the polling, which is all anybody ever wanted from it. A schedule and a
  history are not the same thing: by the time anything suggests retiring, every
  event is in the past, and taking them off would delete the record of a season
  the kids played. This used to cancel everything, which meant the timer
  carefully refused to do something the button then did unconditionally. A
  source with `persists_across_seasons` in its config is never switched off — a
  club team kept across years goes quiet each summer, and disabling it in July
  means noticing in September.
- **`retire.live_events` is upcoming-only, and `repo.tracked_events` is not.**
  They answer different questions. "What is on the calendar from this source"
  counts everything uncancelled; "is there anything left that calsync might
  still need to act on", which is what gates `forget_source`, counts only what
  has not happened. A game played in April cannot be stranded by dropping the
  row — nothing will ever poll that feed again — where an upcoming one would be.
- **Matrix is outbound only.** `matrix.py` + `digest.py` let calsync talk to a
  room; nothing reads from it, and `docs/MATRIX.md` §7 records exactly which
  arrow is built and what the inbound half is blocked on (no API, no identity
  model, no blast-radius policy). Do not let the settings page or the docs imply
  proposals or approvals exist.
- **A digest reads the receipt; it never reads the calendar back and never
  re-parses a feed.** Pulling a rendered title apart is the refusal
  `docs/API.md` gives Hermes and it applies here too. Re-parsing the feed is
  subtler and was how this worked until `event_content` existed: it reports what
  the *feed* says, which after a held or failed poll is not what is on anybody's
  phone — a message announcing a game the calendar does not have is wrong in the
  direction that gets somebody driving to a field. `digest.collect` takes no
  fetcher and touches no network. It still **writes nothing at all**, and a test
  snapshots the tables to keep it that way — comparing the database *file* alone
  is not enough, because WAL means a write need not change it.
- **The digest includes paused sources and excludes cancelled events.** Pausing
  stops polling; it does not take an event off the calendar, so omitting it is
  the same silent under-report the `stale` list exists to prevent. Retiring is
  what removes events, and it cancels everything still to come before disabling
  anything — so a phantom stays out by being cancelled, which is the honest
  test. A retired season's *past* events are still on the calendar and still in
  the digest's window if you point it at one, which is correct: they happened.
- **One definition of stale, in `repo.source_freshness`.** The digest and the
  API both use it. A digest saying "all fine" while the API says "stale" would
  be its own wrong answer, and two thresholds drift apart the moment one is
  tuned. A disabled source is never stale: reporting a deliberate pause as a
  fault trains people to ignore the signal.
- **`event_content` is a receipt, not a cache, and it holds what the *source*
  said.** Written in the same call as the placement record, after the target
  accepted the write, so it cannot disagree with the calendar. Its columns are
  the feed's view — never the rendered one — because the calendar will later be
  that layer plus a higher-trust amendment overlay, and a poll must be able to
  rewrite its own layer without reverting one it does not own (`docs/MATRIX.md`
  §4). No `summary` column, no coordinates, and rows age out with
  `sync_window_back_days`.
- **Stored content is checked independently of the feed's hash**, the same way
  placement is. `content_hash` covers the raw feed component, before venue
  enrichment — so teaching an alias or confirming an address changes what an
  event renders to while leaving its hash identical. Without the check the
  correction never reaches anyone's phone. Reported as `refreshed`, not
  `updated`: `updated` means the feed moved something.
- **An event we cannot classify waits; an event missing a pin does not.**
  `Event.unresolved` carries the questions that decide *where an event goes* —
  which calendar, not how it reads — and `routing.collection_for` sends those to
  the `enrichment` collection. Measured on the Hawks feed, guessing put 12 of 20
  events in the wrong calendar, and correcting that later is a **move**, the
  delete-then-create this project treats as the dangerous operation. An
  unresolved *venue* is deliberately not on that list: it costs a map pin, the
  event still carries its location as text, and holding a fixture over it would
  make a game the family needs to know about invisible. Venues go unresolved on
  every fixture in the test set and have never once changed a collection.
- **The review queue notifies once per question, not once per poll.**
  `enrichment.review` fingerprints the *questions* holding events back and
  records it on `sources.review_notified` — so more events arriving against an
  already-announced question is silent, a genuinely new question is not, and
  clearing the queue resets the flag so the next occurrence is news again. Same
  shape as `dormancy_notified`, for the same reason: the poller runs every
  twenty minutes and a per-poll push is muted by lunchtime, which is worse than
  no push at all. `BLOCKING_DIAGNOSTICS` is what keeps unresolved venues out of
  that fingerprint — they hold nothing back, so paging about them would train
  somebody to ignore the signal that does mean events are off the calendar.
- **The room is told the same questions, and told them differently.**
  `enrichment.dispatch` posts to Matrix, tracked on its own
  `sources.review_dispatched` column rather than sharing the push's flag — the
  two have different audiences, and one flag would mean configuring Matrix after
  a queue opened silently skipped it. It is also deliberately *wider*: a venue
  holds no event back so it never pages a human, but resolving one is the best
  use of a model this project has, so it still gets asked (`DISPATCHABLE` vs
  `BLOCKING_DIAGNOSTICS`). A failed post is **not** recorded, unlike a failed
  push: a task that never reaches the agent means the work never happens.
- **Ten fixtures are one question.** `COLLAPSED` folds `unidentified` into a
  single `resolve_activity` task carrying every observed fixture and the same
  ranked candidates `web/gate.py` offers a human, because one activity alias
  answers all of them. Everything else stays per-item — whether "Skills Session"
  is a game says nothing about "Playoff Game2". A human clicking the suggested
  button and an agent taking the first candidate must be choosing from one list.
- **The review gate is structural, not conventional.** `POST
  /v1/tasks/{id}/result` stores an answer as `answered` and there is no
  parameter on it that approves — applying happens in `enrichment.apply`,
  reached only from the console's `/review/<id>/approve`. An agent can put
  something in front of you and has no path to the function that writes it. It
  refuses three things on purpose: an unknown task id (rows are written when a
  question is *dispatched*, so nothing can invent work for a human), a malformed
  answer (refused on the way in, not at approval time, where the error would
  land on the one person who cannot fix it), and a re-answer of a decided task.
- **An approved answer writes the same row a hand-typed one would.**
  `enrichment.apply` calls exactly the `repo` helpers the console's own answer
  forms call, so the agent path and the manual path cannot drift. An approved
  venue answer still leaves `pin_confirmed = 0`: approving an alias is not
  vouching for coordinates.
- **Staging beats enrichment.** A source still being onboarded is already held
  somewhere; splitting its events across two holding calendars would make the
  promotion gate harder to read, not safer. `SyncReport.awaiting_review` matches
  that precedence, so it never reports a hold that is not happening.
- **The gate's answer forms live in one partial** (`web/templates/_answers.tpl`),
  shared by the source page and `/review`. An answer given in one place has to
  write the same row as the same answer given in the other, and one form is the
  only way to guarantee that.
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
- **Radicale holds the only copy of every past season, not the database.**
  `event_content` is pruned to `sync_window_back_days`, and a team feed drops a
  season within months of it ending — so a game played last spring exists in
  exactly one place, the calendar server. `scripts/backup.sh` backs that up
  first and says why. `retire.py` goes out of its way not to delete those
  events; a backup that skipped the `radicale-data` volume would delete them
  anyway, just more slowly.
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
the CalDAV server requirements and acceptance checks — which now include one
comparing what `GET /v1/events` serves against the VEVENTs a real Radicale hands
a real phone, since "the API said 7pm, the calendar says 8pm" is only worth
testing against a calendar this process did not just write itself.

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
- **The API is a separate app for a different reason, not a different port.**
  It serves programs over a bearer token, so it has no `Sec-Fetch-Site` check
  (no cookies, nothing to ride) and it refuses to start without a credential.
  The console's "no login is the right call" reasoning does not transfer; don't
  merge the two apps or give either the other's posture.
- Normalization is deterministic — no model in the parse path, so the same feed
  always renders the same title and any change traces to a config change.
- Commit messages are imperative and explain the *why*, with no conventional-commit
  prefixes: "Move configuration into the database so other households can deploy this".
- Work happens on branches; `main` is the PR target.
