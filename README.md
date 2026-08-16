# calsync

Family calendar sync platform: pull kids' schedules out of youth-sports apps,
PDFs, and email; normalize them into one source of truth; push them into
iCloud calendars.

Status: **planning**. See [docs/PLAN.md](docs/PLAN.md) for the architecture,
gap analysis, and phased roadmap, and [docs/API.md](docs/API.md) for the
write API that agents and pollers code against. [docs/NAMING.md](docs/NAMING.md)
covers event title and location conventions, and
[docs/MATCHING.md](docs/MATCHING.md) covers dedup and adopting the hand-created
events already in the shared calendars.
[docs/MATRIX.md](docs/MATRIX.md) covers the Matrix room where calsync, Hermes,
and you interact. [docs/sources/](docs/sources/) holds per-source adapter
notes from the Phase 0 survey, [docs/deployment/](docs/deployment/) holds
infrastructure requirements, and [docs/config.example.yaml](docs/config.example.yaml)
shows a real feed wired up end to end.

## Shape

```[text]
Hermes (PDF/photo) ─┐                                    ┌─→ iCloud: Games
email worker       ─┼─→ calsync API ─→ Radicale (CalDAV) ─┼─→ iCloud: Practices
ICS pollers        ─┤   (only writer)  SQLite (raw docs, ─┤   (shared w/ family)
Matrix bot / paste ─┤                  proposals, sync)  └─→ ICS feeds
web UI (feeds)     ─┘        │
                       review queue
```

The API is the only writer. Agents submit *proposals*; the API decides what
reaches the calendar. calsync manages sourced events only — hand-created
appointments are never touched.

Matrix room for the daily loop (capture, approve, amend). Web UI for setup
(add a feed, bind it to a kid and sport).

## Running it

```bash
calsync --db drive.db init-db
calsync --db drive.db import config.yaml
calsync --db drive.db sync --out ./out --dry-run   # then drop --dry-run
calsync --db drive.db status
```

`--out` writes a directory of `.ics` files — safe to point at a live feed, and
the output diffs in git. Without it, events go to the CalDAV server configured in
settings.

## Docker

```bash
cp -r deploy/radicale/. config/radicale/     # then edit users + rights
htpasswd -B -c config/radicale/users calsync
mkdir -p secrets && printf '{"radicale_password":"..."}' > secrets/secrets.json
chmod 600 secrets/secrets.json

docker compose up -d                                    # Radicale + the poller
docker compose run --rm calsync import /config/calsync.yaml
docker compose run --rm calsync stage tr-hawks          # onboarding calendar
docker compose run --rm calsync promote tr-hawks        # when the parse is clean
```

Radicale is bound to loopback — reach it over Tailscale, not by opening the
port ([docs/deployment/radicale.md](docs/deployment/radicale.md) §5). Onboarding
flow: [docs/ONBOARDING.md](docs/ONBOARDING.md).

## Next step

The web config UI, built around [docs/ONBOARDING.md](docs/ONBOARDING.md): rec
teams are recreated every season, so onboarding a feed is recurring work rather
than one-time setup. Player360 and TeamReach are verified against live feeds;
the flag-football app is the last unsurveyed source, and the only one whose UIDs
are not stable.
