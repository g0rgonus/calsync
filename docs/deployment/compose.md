# The compose stack

Reference for `docker-compose.yml`. Server requirements are in `radicale.md`,
routing in `proxy.md`.

## First run

In a directory with nothing in it:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/out" \
  ghcr.io/g0rgonus/calsync:release init-deploy /out
docker compose up -d
```

`init-deploy` writes the compose file, `config/radicale/{config,rights}`,
`config/caddy/Caddyfile` and a `.env` holding three generated secrets:
`CALSYNC_SECRET_RADICALE_PASSWORD`, `..._READER_PASSWORD` and
`..._API_TOKEN`. The stack will not start without them — Radicale's container
refuses, naming the variable.

Generated once, on the host, by whoever runs the command. `.env` stays the only
place a credential is written, and `init-deploy` never overwrites, so running it
again after an upgrade rotates nothing and re-subscribes no device.

From a checkout it is the same command run from the repo —
`.venv/bin/calsync init-deploy .`, which takes the assets from `deploy/` rather
than the image. `scripts/dev-stack.sh` does that plus the demo feed server.

The read-only password is the one a phone needs:

```bash
grep CALSYNC_SECRET_RADICALE_READER_PASSWORD .env
```

One port is published, routed by path:

| | |
|---|---|
| `http://localhost:8730/` | the console |
| `http://localhost:8730/cal/` | Radicale — what a phone subscribes to |
| `http://localhost:8730/v1` | the read API |

The publish is not an access control. The console has no login; whatever VPN or
authenticating proxy fronts the host is what stands in front of it.

## Services

| | |
|---|---|
| `radicale` | The calendar server. Not published: reached at `/cal` through `proxy`, and directly at `http://radicale:5232` by calsync. |
| `calsync` | The poller. One-off commands use the same service — `docker compose run --rm calsync status`, `… stage tr-otters`, `… import /config/calsync.yaml`. |
| `web` | The console, port 8730 in-container. It and the poller are two writers on one SQLite file; `db.connect` sets the busy timeout that allows for that. |
| `api` | The read API, port 8731 in-container, served at `/v1`. `CALSYNC_API_REPLICAS=0` turns it off. |
| `proxy` | Caddy. The only published port. |
| `feeds` | `--profile demo` only. Replays the recorded fixtures with their dates shifted onto this week. Needs a checkout — it mounts `demo/` and `tests/fixtures/`. |

There is no init container. Every service starts, stays up, and is expected to
be running — a stack with something permanently exited reports as broken in
anything watching it.

## Credentials

Two Radicale accounts: `calsync` writes, `calreader` is read-only and is what
goes on a family's devices.

Radicale authenticates against an htpasswd file and cannot read an environment
variable — no backend it ships does. So its container writes that file itself,
from `.env`, on every start:

```sh
printf 'calsync:%s\ncalreader:%s\n' "$CALSYNC_SECRET_..." "$CALSYNC_SECRET_..." > /tmp/users
exec /venv/bin/radicale --config /config/config
```

Three properties follow, and they are the reason it is done this way:

- **Derived, never stored.** The file is rebuilt from `.env` at every start, so
  it cannot drift from the password the poller uses. There is no second copy to
  keep in step and nothing to re-derive after an edit.
- **Never on the host.** It lives in `/tmp` inside the container at 0600. The
  only place a calendar password exists on the machine is `.env`.
- **Nothing is rotated.** Changing `.env` and restarting changes the password;
  leaving it alone leaves it alone. A device stays subscribed until you choose
  otherwise.

`plain` rather than bcrypt: the file is derived from plaintext in `.env` and
never leaves the container, so hashing it would protect against nothing the
environment does not already expose.

**`user: "2999:2999"` on the radicale service is load-bearing.** That image's
entrypoint only drops privileges when told to run radicale directly, and this
stack gives it a shell — so without that line Radicale runs as root.

### The one secret that cannot come from `.env`

Feed tokens. They arrive by pasting a team's URL during onboarding, one per
team, and nobody knows them before startup. `onboarding.templatise` moves the
token out of the URL into the secret store, so a source row stays safe to read
and export.

That store is `/data/secrets.json` — beside the database, in the volume that is
already the console's state. No host directory, so no ownership to get wrong.

## Configuration

Two prefixes:

| | |
|---|---|
| `CALSYNC_SETTING_<KEY>` | Seeds a row in the `settings` table when the database is created. Any key in `db.DEFAULT_SETTINGS`; an unknown key raises. Seed-only — a later edit in the console wins. |
| `CALSYNC_SECRET_<REF>` | Resolves a credential. Checked before the secret store. |

`.env` reaches Compose for `${...}` substitution but not the containers;
`env_file` carries it in. It reads `.env` and nothing else — a variable exported
in the shell reaches a container only if it is declared under `environment:`.

`env_file` is all-or-nothing: everything in `.env` reaches every calsync
service. Scoping a secret to one service means an explicit `environment:` entry
there, which is how `radicale` gets exactly the two passwords it needs.

### Variables

The first three are required.

| | |
|---|---|
| `CALSYNC_SECRET_RADICALE_PASSWORD` | calsync's own account. |
| `CALSYNC_SECRET_RADICALE_READER_PASSWORD` | The `calreader` account — read-only, for phones and Radicale's web UI. |
| `CALSYNC_SECRET_API_TOKEN` | The read API's bearer token. |
| `CALSYNC_API_REPLICAS` | `0` turns the read API off. Defaults to 1. |
| `CALSYNC_SETTING_RADICALE_URL` | Only to point calsync at a CalDAV server it does not run. |
| `CALSYNC_SETTING_MATRIX_*`, `CALSYNC_SECRET_MATRIX_ACCESS_TOKEN` | All four or none. Outbound only (`docs/MATRIX.md` §7). |
| `CALSYNC_SETTING_DIGEST_SEND_AT` | `HH:MM` local. Empty means never. The poller carries it; there is no cron. |
| `CALSYNC_SECRET_PUSHOVER_TOKEN`, `_USER` | Pushes. |
| `CALSYNC_SETTING_DEFAULT_TZ`, `_TITLE_TEMPLATE`, `_COLLECTION_TEMPLATE` | Household conventions. Full list: `DEFAULT_SETTINGS` in `src/calsync/db/__init__.py`. |
| `CALSYNC_TAG` | `release` (default), `latest`, `dev`, `v0.2`, `v0.2.0`, `sha-1a2b3c4`. |

## Mounts

- **`./config/radicale` is the directory, not the files in it.** Docker creates
  a bind mount whose source does not exist as a *directory*.
- **`calsync-data` is read-write for the read-only API.** WAL needs `-wal` and
  `-shm` beside the database, and `open_db` migrates on startup. It also holds
  the secret store.
- **`./config/calsync` is only for `calsync import`.** Configuration lives in
  the database.
- **`image:` and `build:` are both declared.** Compose pulls the published image
  when one exists, so a checkout testing its own changes needs
  `docker compose up -d --build`.

## Healthchecks

- `api` is healthy on **401**, which proves the app serves *and* that the auth
  hook is in front of it.
- `web` fetches its own root, exercising the template layer.
- `proxy` has none, and no `depends_on` on what it fronts: Caddy resolves
  upstreams per request, so a service that is down is a 502 on its own path.

## Upgrading

```bash
docker compose pull && docker compose up -d
```

Enough when only the image moved. When the compose file moved — a new service, a
changed port — `init-deploy` will not overwrite what is already on disk, so
delete what you want re-issued first:

```bash
rm docker-compose.yml config/radicale/config
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/out" \
  ghcr.io/g0rgonus/calsync:release init-deploy /out
docker compose pull && docker compose up -d
```

`.env`, `rights` and both volumes are untouched by that, and no password
changes — devices stay subscribed.

To run a build that has no version yet, pin the immutable `sha-` tag —
`CALSYNC_TAG=sha-1a2b3c4`. Not `dev`, which repoints to whichever branch was
pushed last.
