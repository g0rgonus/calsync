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

Matrix room for the daily loop (capture, approve, amend). The web console does
onboarding: paste a feed URL, bind it to a kid and a sport, and answer the
questions the parse could not. It edits SQLite directly rather than going
through the API — see "Configuration is not in this API" in
[docs/API.md](docs/API.md).

## Running it

```bash
calsync --db drive.db init-db
calsync --db drive.db import config.yaml
calsync --db drive.db sync --out ./out --dry-run   # then drop --dry-run
calsync --db drive.db status
calsync --db drive.db web                          # console on localhost:8730
```

`--out` writes a directory of `.ics` files — safe to point at a live feed, and
the output diffs in git. Without it, events go to the CalDAV server configured in
settings.

## Docker

Published to `ghcr.io/g0rgonus/calsync`. To stand up a stack **without cloning
this repo** — the image carries its own compose file and server config:

```bash
docker login ghcr.io                       # while this repo is private
mkdir calsync && cd calsync
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD:/out" ghcr.io/g0rgonus/calsync:latest init-deploy /out
```

That writes `docker-compose.yml` and `config/radicale/{config,rights}`, then
prints the remaining steps. It never overwrites a file you have edited.

From a checkout instead:

```bash
cp -r deploy/radicale/. config/radicale/     # then edit users + rights
htpasswd -B -c config/radicale/users calsync
htpasswd -B    config/radicale/users calreader

mkdir -p secrets && printf '{"radicale_password":"..."}' > secrets/secrets.json
chmod 600 secrets/secrets.json
sudo chown -R 10001:10001 secrets            # Linux only — see below

docker compose up -d radicale
docker compose run --rm calsync set radicale_url http://radicale:5232
docker compose run --rm calsync check        # must pass before going further
docker compose up -d
```

**The last three commands are not optional.** `radicale_url` ships as
`http://localhost:5232`, which is right when calsync runs on the host and wrong
inside a container, where localhost *is* the container. A stack left that way
comes up healthy, writes nothing, reports it once per event, and then backs off
to hours — which is exactly how it went unnoticed on the first real deployment.
`check` asks the question directly, and the console has the same button on
`/settings`.

**The `chown` is Linux only, and it is not cosmetic.** The image runs as uid
10001 and the secret store refuses a file any other account can read, so the
file has to be both `600` and owned by that uid. A Linux bind mount preserves
host ownership; macOS presents the mount as the container user and hides the
problem entirely, so a stack that works on a laptop fails on the box it deploys
to. CI follows these steps on Linux to keep them honest.

`docker compose --profile api up -d api` adds the read API — opt-in, because its
only consumer is an agent that does not exist yet.

## Next step

The flag-football app is the last unsurveyed source, and the only one whose UIDs
are not stable — which duplicates rather than deletes, so it needs the identity
guard in `diff.py` exercised against a real feed before it is trusted.
