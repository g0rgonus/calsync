# calsync

Keeps a family's shared calendar in step with the kids' team schedules. It polls
the feeds those teams publish, normalizes what coaches typed into something
consistent, and writes the result to a CalDAV calendar the family's phones
already subscribe to.

The failure it exists to prevent is a specific one: **a duplicate in a shared
calendar**. Two copies of a game at different times, or a cancelled fixture that
never went away, and somebody drives to the wrong field. Most of the design
below is about not doing that — which is why absence is treated as a suspicious
signal, why a reclassified event is a move rather than an update, and why
nothing calsync did not create is ever touched.

## Status

The sync loop runs end to end and has been in daily use against live feeds.

**Built and exercised:** two source adapters (Player360, TeamReach), the diff
and its safety guards, rendering, CalDAV and `.ics` directory targets, a
continuous poller with per-source backoff, the onboarding console, a read-only
HTTP API, a Matrix daily digest, dormancy and retirement handling, and a Docker
stack with published images. 515 tests, plus CI jobs that stand up a real
Radicale and follow the documented first-run steps on Linux.

**Specified but not built:** everything in [docs/API.md](docs/API.md) past the
two read endpoints — documents, proposals, approvals, `PATCH` and the amendment
overlay — along with [docs/MATCHING.md](docs/MATCHING.md) and the inbound half
of [docs/MATRIX.md](docs/MATRIX.md). Read those as the design contract, not as a
description of the code. The unbuilt API paths answer `501` with the reason
rather than `404`, because "not yet" and "you have the URL wrong" are different
answers. [docs/PLAN.md](docs/PLAN.md) is the original architecture and gap
analysis, kept as written.

The Google Calendar target is **withdrawn as a destination**: the payload
builder is complete and tested, but the OAuth exchange is missing
([#1](https://github.com/g0rgonus/calsync/issues/1)), and an entry in a dropdown
reads as a supported choice however the caption is worded.

This is one household's tool rather than a product, but nothing about that
household is in the code — calendar splitting, title format, multi-kid style and
the safety thresholds are all rows in a `settings` table, and
`tests/test_configurable.py` asserts it by configuring a different family and
expecting their conventions.

## Shape

```[text]
TeamReach   ─┐                                    ┌─→ games        ─┐  on the
Player360   ─┼─→  calsync  ───→  CalDAV  ─────────┼─→ practices    ─┘  family's
(ICS feeds) ─┘    parse          (Radicale)       │                     phones
                  diff ← guards                   ├─→ onboarding   ─┐  held for
                  render                          └─→ enrichment   ─┘  a human
                    │
                    ├─→ web console   onboard a feed, answer what the parse
                    │                 could not, approve, configure, and see
                    │                 the month calsync has written
                    ├─→ read API      bearer token, for agents
                    └─→ Matrix        daily digest, outbound only
```

One direction, five stages, plain dataclasses across each boundary. `sync.py` is
the only module that closes the loop, and its ordering is the safety: a failed
fetch aborts before the diff, and placement is recorded only after the target
accepts the write.

Events calsync **cannot classify** are held in an `enrichment` collection rather
than filed under a guess — visible in a calendar client, tracked, and released
by a human answering the question in `/review`. Measured on one real feed,
guessing put 12 of 20 events in the wrong calendar, and correcting that later is
the delete-then-create this project treats as the dangerous operation.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                # 537 tests, ~5s
```

```bash
calsync --db drive.db init-db
calsync --db drive.db import config.yaml        # docs/config.example.yaml is the shape
calsync --db drive.db sync --out ./out --dry-run
calsync --db drive.db sync --out ./out
calsync --db drive.db poll                      # the long-running loop
calsync --db drive.db status                    # per-source health + recent polls
calsync --db drive.db check                     # can the configured calendar be reached?
calsync --db drive.db web                       # console on localhost:8730
calsync --db drive.db api                       # read API on localhost:8731
```

`--out` writes a directory of `.ics` files — safe to point at a live feed, and
the output diffs in git. Without it, events go to the CalDAV server configured
in settings. `--from-file` replays a saved payload without a credential, and
`--now <iso>` pins the clock for reproducible runs. Exit codes are meaningful:
`0` ok, `1` error, `3` a guard held — a scheduler should alert on `3` without
treating it as a crash.

Adding a feed goes through the console (paste the URL, confirm three things) or,
equivalently, the CLI ([docs/ONBOARDING.md](docs/ONBOARDING.md)):

```bash
calsync --db drive.db stage tr-otters     # routes to an onboarding collection
calsync --db drive.db promote tr-otters   # gated on a clean parse + a seen fixture
```

Secrets never live in the database. `sources.url_template` stores
`{{secret:p360_token}}`; the value comes from `CALSYNC_SECRET_P360_TOKEN` or
`~/.config/calsync/secrets.json`, which must be `chmod 600`.

To try the console without a real team's feed, `docker compose --profile demo up
-d web feeds` serves the recorded fixtures with their dates shifted onto this
week.

## Docker

Published to `ghcr.io/g0rgonus/calsync` for `linux/amd64` and `linux/arm64`, so
a Raspberry Pi or an Apple Silicon Mac is as good a host as an x86 box.

Three moving tags, because one cannot mean "what I run", "what is merged" and
"what I would hand a stranger" at the same time: **`release`** is the newest
tagged version and what a deployment should track, **`latest`** is `main`, and
**`dev`** is whatever branch is being worked on. `v0.2` pins a minor line,
`v0.2.0` pins outright, and every build gets a `sha-` tag for bisecting.
Compose defaults to `release`; `CALSYNC_TAG` in `.env` changes it.

The whole first run, in a directory with nothing in it:

```bash
mkdir calsync && cd calsync
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD:/out" ghcr.io/g0rgonus/calsync:release init-deploy /out
docker compose up -d
```

`init-deploy` writes `docker-compose.yml`, `config/radicale/{config,rights}`,
`config/caddy/Caddyfile` and a `.env` with the three secrets the stack needs,
generated. It never overwrites a file you have edited — run it again after an
upgrade and it keeps everything, including `.env`, so no password rotates and no
device re-subscribes.

The read-only password is the one a phone needs:

```bash
grep CALSYNC_SECRET_RADICALE_READER_PASSWORD .env
```

The stack publishes **one port**, and routes by path behind it:

| | |
|---|---|
| `http://localhost:8730/` | the console |
| `http://localhost:8730/cal/` | Radicale — this is what a phone subscribes to |
| `http://localhost:8730/v1` | the read API |

One address, one certificate, one thing to put a VPN in front of. The three
services share a browser origin as a result, and the CalDAV route needs the
server's cooperation rather than a plain proxy pass — both in
`docs/deployment/proxy.md`.

A checkout is the same two commands — `init-deploy` reads its assets from the
repo when it is run from one (`.venv/bin/calsync init-deploy .`), or
`scripts/dev-stack.sh` does it plus the demo feed server.

It used to be eight commands, and each one that went away was a step somebody
could get wrong quietly:

- **`radicale_url`** ships as `http://localhost:5232` — right when calsync runs
  on the host, wrong inside a container, where localhost *is* the container. A
  stack left that way came up healthy, wrote nothing, reported it once per
  event, and then backed off to hours, which is how it went unnoticed on the
  first real deployment. Compose now sets `CALSYNC_SETTING_RADICALE_URL`, and
  the poller verifies the calendar before it will start rather than running on
  and reporting it in passing.
- **The credentials** were `htpasswd -B` twice, a hand-written JSON file and a
  `chown`. They are three lines in `.env` now. Radicale cannot read an
  environment variable, so its own container writes the htpasswd file from those
  values at every start, into `/tmp` at 0600 — derived rather than stored, so it
  cannot drift, and no calendar password is ever written to the host.

`.env` is written for you by `init-deploy`, secrets and all. To choose them
yourself, or to configure Matrix, Pushover or the household conventions, edit it
before the first `up` — `.env.example` documents every key.

Everything in there goes through one of two prefixes, both of which already
existed for their own reasons:

| | |
|---|---|
| `CALSYNC_SETTING_<KEY>` | Seeds a row in the `settings` table when the database is created. Any key in `DEFAULT_SETTINGS` works. |
| `CALSYNC_SECRET_<REF>` | Resolves a credential. The secret store has always read the environment before the file. |

So Matrix, Pushover and the read API's bearer token are configurable before
startup without a line of code specific to any of them — as is every household
convention, from `title_template` to `default_tz`. Three rules hold across all
of it, and they are the interesting part:

- **Empty means unset.** Compose passes every variable it declares, filled in
  or not, so seeding the empty string would replace a working default with
  nothing on a deployment that configured neither.
- **Settings seed, they never override.** A variable that reasserted itself on
  every restart would silently undo an edit made in the console, and the console
  is where a person looks. A mismatch is logged instead. An unknown key raises
  rather than being ignored, because a typo that quietly does nothing is the
  failure this exists to remove.
- **Nothing is rotated.** The users file is *derived* from whatever passwords
  are supplied or already stored, re-derived whenever they disagree, and never
  regenerated on its own — a fresh password would lock out every phone already
  subscribed. An account added by hand is left alone, and a supplied password is
  never copied into the secrets file: somebody who kept a credential out of a
  file on disk did not ask for it to be written to one.

Feed tokens are deliberately not part of this. They arrive by pasting a team's
URL during onboarding, one per team, and there is no point before startup at
which anybody knows them.

A deployment that has synced before gets a warning rather than a refusal when
the calendar is unreachable — by then the address is known to have worked, so a
failure is far likelier to be a brief outage than a mistake, and the sync loop
already handles that with backoff. `calsync check` asks the question directly at
any time, and the console has the same button on `/settings`. CI follows the
documented first run on Linux, which is where the ownership problems live.

The read API is part of the stack, at `/v1`. It refuses to serve without a
bearer token, which is the third value in `.env`. `CALSYNC_API_REPLICAS=0`
turns it off.

Any CalDAV server meeting the R1–R8 contract in
[docs/deployment/radicale.md](docs/deployment/radicale.md) will do; Radicale is
the one this was verified against, and CI re-verifies it on every push.

## Invariants worth knowing before you change anything

These encode failure modes found in real feeds. Weakening one of them can wipe a
family calendar, so they are contracts rather than defaults — the full list is
in [CLAUDE.md](CLAUDE.md), with the reasoning in `docs/`.

- **Absence is the only cancellation signal**, so a bad fetch looks exactly like
  a cancelled season. Cancellations are withheld when more than 20% or more than
  3 tracked future events vanish in one poll.
- **A feed's UID may not be stable**, and that failure duplicates rather than
  deletes. One observed source embeds a generation timestamp in every UID, so a
  second guard withholds both halves on total identity turnover.
- **Change detection uses our own content hash**, never upstream `SEQUENCE` —
  which in the wild is variously a churning mtime, an honest mtime, and a
  non-monotonic flag that decrements.
- **An empty or unparseable feed raises**, because downstream, zero events is
  indistinguishable from everything being cancelled.
- **Feeds have no format — coaches type them.** Three teams on one platform in
  one season used three incompatible conventions, so adapters read by strategy
  and report the misses rather than assuming a shape.
- **calsync manages sourced events only.** Hand-created family appointments are
  never modified or deleted.
- **A model is the last tier of venue resolution, never the first**, and nothing
  a model proposes is trusted until a human confirms it. The parse path is fully
  deterministic: the same feed always renders the same title.
- **A finished season is switched off, never erased.** By the time anything
  suggests retiring a team, every event is in the past, and removing them would
  delete the record of a season the kids played.

## Fixtures are invented, always

Every name, team, venue and address under `tests/fixtures/` is made up, and that
is a rule rather than an accident. These files started as recordings of real
feeds, which made them a location history for real children — dates, times and
street addresses of where a specific family's kids would be. Useful test data,
and a thing you cannot publish.

Scrubbing them is not search-and-replace, because the *shapes* are the tests: a
comma before the city and none before the state, a field designator with and
without a space, three names for one place, trailing spaces exactly as they were
typed. If a golden test fails after you edit a fixture, you have probably
changed a shape rather than a name — read `docs/sources/<source>.md` before
touching the assertion.

## Docs

`docs/` carries the reasoning the code cannot.

| | |
|---|---|
| [ONBOARDING.md](docs/ONBOARDING.md) | Adding a feed, and the gate that promotes it |
| [API.md](docs/API.md) | The HTTP contract, and why configuration is not in it |
| [NAMING.md](docs/NAMING.md) | Title and location conventions |
| [MATCHING.md](docs/MATCHING.md) | Dedup and adoption — specified, not built |
| [MATRIX.md](docs/MATRIX.md) | The room, the digest, and which arrow exists |
| [PLAN.md](docs/PLAN.md) | The original architecture and gap analysis |
| [deployment/compose.md](docs/deployment/compose.md) | Why the stack is shaped like this — first run, passwords, the uid that bites |
| [deployment/proxy.md](docs/deployment/proxy.md) | The one published port, and why /cal is not a plain reverse proxy |
| [deployment/radicale.md](docs/deployment/radicale.md) | CalDAV requirements and acceptance checks |
| [sources/](docs/sources/) | Per-source traps, one-to-one with the golden tests |

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs are welcome, with two asks: keep it stdlib-first — three runtime
dependencies total, and a new one needs a justification — and explain the *why*
in commit messages rather than the what. Work happens on branches; `main` is the
PR target. There is no linter or formatter configured; please do not add one
uninvited.
