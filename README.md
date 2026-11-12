# calsync

[![ci](https://github.com/g0rgonus/calsync/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/g0rgonus/calsync/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/g0rgonus/calsync)](https://github.com/g0rgonus/calsync/releases)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![python](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)
![image](https://img.shields.io/badge/image-ghcr.io-2496ED?logo=docker&logoColor=white)

calsync turns the team schedules coaches publish into one family calendar:
consistent titles, games and practices split, written to a CalDAV server the
family's phones already subscribe to. It only ever changes events it created,
and when a feed looks wrong, or it can't tell what an event is, it holds and
asks a person rather than guessing.

A title reads like `Luke ⚽️ vs Rimward` or `Lu+Le 🏊 Practice`. What calsync
writes, and the settings that change it, are in [docs/events.md](docs/events.md).

## Connectors

**Sources**

- [360Player](docs/connectors/360player.md): a team's ICS feed
- [TeamReach](docs/connectors/teamreach.md): a team's ICS feed
- [Calendar feed](docs/connectors/calendar-feed.md): a club, troop or school calendar's
  public iCal export, routed to calendars by category
- [Pushed](docs/connectors/pushed.md): an agent or script sends the events over
  the inbound API

**Destinations**

- CalDAV: Radicale is bundled in the stack
- A directory of `.ics` files (`--out`)
- Apple Calendar, through the [Mac mirror](mac/README.md)

## How it works

```text
360Player   ─┐                                    ┌─→ games        ─┐  on the
TeamReach   ─┼─→  calsync  ───→  CalDAV  ─────────┼─→ practices    ─┘  family's
Pushed      ─┘    parse          (Radicale)       │                     phones
                  diff ← guards                   ├─→ onboarding   ─┐  held for
                  render                          └─→ enrichment   ─┘  a person
                    │
                    ├─→ console      onboard a team, answer what the parse
                    │                could not, see what was written
                    ├─→ read API     bearer token, for agents
                    └─→ Matrix       daily digest, outbound only
```

Each poll parses a feed, compares it with what calsync last wrote, and writes
the difference. A poll where too much vanished, or every id changed, is held
rather than applied. An event calsync cannot place waits in `enrichment` until
someone answers on the console's `/review`. A new team writes to `onboarding`
until it is promoted.

## Run it with Docker

In an empty directory:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/out" \
  ghcr.io/g0rgonus/calsync:latest init-deploy /out
docker compose up -d
```

`init-deploy` writes `docker-compose.yml`, the Radicale and Caddy config, and a
`.env` with the three secrets the stack needs, generated. It never overwrites a
file that is already there, so running it again after an upgrade changes no
password. `.env.example` documents every other setting.

The stack publishes one port:

| | |
|---|---|
| `http://<host>:8730/` | the console |
| `http://<host>:8730/cal/` | Radicale, for phones |
| `http://<host>:8730/v1` | the read API |

The console has no login. Run the stack on a private network, and put whatever
you trust in front of it.

**Subscribing a phone.** Add a CalDAV account at `http://<host>:8730/cal/` as
`calreader`, with the password from:

```bash
grep CALSYNC_SECRET_RADICALE_READER_PASSWORD .env
```

Or set `CALSYNC_RADICALE_ANONYMOUS_READ=1` in `.env` and subscribe to a bare
URL such as `http://<host>:8730/cal/calsync/games/` with no password. Anyone who
can reach the port can then read the calendars. Writing still needs calsync's
own credential.

Images are published for `linux/amd64` and `linux/arm64`. Compose runs
`latest`, the newest release. To pin one, set `CALSYNC_TAG=1.0.0` in `.env`;
upgrading is changing it and running `docker compose up -d`. Every tag is listed
in [CONTRIBUTING.md](CONTRIBUTING.md#workflow-and-releases), and
[CHANGELOG.md](CHANGELOG.md) says what changed.

## Run it from a checkout

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Try a feed without touching a calendar server:

```bash
calsync --db drive.db init-db
calsync --db drive.db web                       # the console, on localhost:8730
calsync --db drive.db sync --out ./out --dry-run
```

`--out` writes a directory of `.ics` files instead of using CalDAV, so the
result diffs in git. `calsync --help` lists the rest.

To try the console with no real team, `docker compose --profile demo up -d web
feeds` replays the test fixtures with their dates moved onto this week.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
