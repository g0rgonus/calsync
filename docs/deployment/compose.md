# The compose stack

Reference for `docker-compose.yml`. Server requirements are in `radicale.md`,
routing in `proxy.md`.

## First run

```bash
docker compose up -d
```

No `.env` and no arguments. `bootstrap` writes Radicale's config, rights and
users file, the Caddyfile, and generates the two calendar passwords and the
API's bearer token. It prints the read-only password and the token once:

```bash
docker compose logs bootstrap
```

One port is published, routed by path:

| | |
|---|---|
| `http://localhost:8730/` | the console |
| `http://localhost:8730/cal/` | Radicale — what a phone subscribes to |
| `http://localhost:8730/v1` | the read API |

The publish is not an access control. The console has no login; whatever VPN or
authenticating proxy fronts the host is what stands in front of it.

## Upgrading

```bash
docker compose pull && docker compose up -d
```

That is enough when only the image moved. **It is not enough when the compose
file moved** — a new service, a changed port — because `init-deploy` never
overwrites, so it keeps the `docker-compose.yml` and server config already on
disk and the upgrade silently does nothing.

To take a new compose file, delete what you want re-issued first:

```bash
rm docker-compose.yml config/radicale/config
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/out" \
  ghcr.io/g0rgonus/calsync:release init-deploy /out
docker compose pull && docker compose up -d
```

`config/radicale/config` is worth including: `bootstrap` keeps an existing one
forever, so a change to the shipped server config never reaches a deployment
that already has one. `rights`, `.env` and the users file are untouched by this,
and no password is rotated — devices stay subscribed.

The compose file and the image ship together, so a compose file ahead of its
image expects files that image's `bootstrap` does not write. The symptom is a
crash-looping `proxy` with `open /etc/caddy/Caddyfile: no such file or
directory`, and `bootstrap` printing neither `wrote` nor `kept` for the
Caddyfile. Pulling the matching image fixes it; reverting the compose file does
too.

To run a build that has no version yet, pin the immutable `sha-` tag —
`CALSYNC_TAG=sha-1a2b3c4`. Not `dev`, which repoints to whichever branch was
pushed last.

In a checkout, `docker compose up -d` **pulls** the published image rather than
building your changes. Testing local changes needs `--build`.

## Services

| | |
|---|---|
| `bootstrap` | One-shot, root. Writes server config and credentials, then exits; everything else waits on it. Idempotent — never overwrites an edited file, never rotates a password. |
| `radicale` | The calendar server. Not published: reached at `/cal` through `proxy`, and directly at `http://radicale:5232` by calsync. |
| `calsync` | The poller. One-off commands use the same service — `docker compose run --rm calsync status`, `… stage tr-otters`, `… import /config/calsync.yaml`. |
| `web` | The console, port 8730 in-container. It and the poller are two writers on one SQLite file; `db.connect` sets the busy timeout that allows for that. |
| `api` | The read API, port 8731 in-container, served at `/v1`. Refuses to start without a bearer token; `bootstrap` generates one and prints it once. `CALSYNC_API_REPLICAS=0` turns it off. |
| `proxy` | Caddy. The only published port. |
| `feeds` | `--profile demo` only. Replays the recorded fixtures with their dates shifted onto this week. Needs a checkout — it mounts `demo/` and `tests/fixtures/`. |

## Configuration

Two prefixes:

| | |
|---|---|
| `CALSYNC_SETTING_<KEY>` | Seeds a row in the `settings` table when the database is created. Any key in `db.DEFAULT_SETTINGS`; an unknown key raises. Seed-only — a later edit in the console wins. |
| `CALSYNC_SECRET_<REF>` | Resolves a credential. Checked before the secrets file. |

`.env` reaches Compose for `${...}` substitution but not the containers;
`env_file` carries it in, marked `required: false`. The three entries under
`environment:` are not from `.env` — `CALSYNC_DB` and `CALSYNC_SECRETS` are
container paths, and `CALSYNC_SETTING_RADICALE_URL` overrides a code default of
`localhost:5232`, which inside a container is the container.

`env_file` is all-or-nothing: everything in `.env` reaches every calsync
service. Scoping a secret to one service means an explicit `environment:` entry
there.

**`env_file` reads `.env` and nothing else.** A variable exported in the shell —
`CALSYNC_BOOTSTRAP_OWNER_UID=0 docker compose up -d` — reaches a container only
if it is declared under `environment:` as `${VAR:-default}`, which is why that
one is. Everything else goes in `.env`.

### Variables

All optional.

| | |
|---|---|
| `CALSYNC_SECRET_RADICALE_PASSWORD` | calsync's own account. Generated if unset. |
| `CALSYNC_SECRET_RADICALE_READER_PASSWORD` | The `calreader` account — read-only, for phones and Radicale's web UI. Generated if unset, and printed once. |
| `CALSYNC_SETTING_RADICALE_URL` | Only to point calsync at a CalDAV server it does not run. |
| `CALSYNC_SETTING_MATRIX_*`, `CALSYNC_SECRET_MATRIX_ACCESS_TOKEN` | All four or none. Outbound only (`docs/MATRIX.md` §7). |
| `CALSYNC_SETTING_DIGEST_SEND_AT` | `HH:MM` local. Empty means never. The poller carries it; there is no cron. |
| `CALSYNC_SECRET_PUSHOVER_TOKEN`, `_USER` | Pushes. |
| `CALSYNC_SECRET_API_TOKEN` | The read API's bearer token. Generated if unset, and printed once. |
| `CALSYNC_API_REPLICAS` | `0` turns the read API off. Defaults to 1. |
| `CALSYNC_SETTING_DEFAULT_TZ`, `_TITLE_TEMPLATE`, `_COLLECTION_TEMPLATE` | Household conventions. Full list: `DEFAULT_SETTINGS` in `src/calsync/db/__init__.py`. |
| `CALSYNC_BOOTSTRAP_OWNER_UID` | Who owns the secrets file. Defaults to 10001; `0` leaves it alone. |
| `CALSYNC_TAG` | `release` (default), `latest`, `dev`, `v0.2`, `v0.2.0`, `sha-1a2b3c4`. |

Feed tokens are not here — they arrive by pasting a team's URL during
onboarding, one per team.

## Credentials

Two accounts: `calsync` writes, `calreader` is read-only and is what goes on a
family's devices. `bootstrap` also generates the API's bearer token, under
`api_token`.

Radicale authenticates against an htpasswd file and cannot read an environment
variable, so a password set in `.env` still has to reach `config/radicale/users`
as a bcrypt line. `bootstrap` derives that file from whatever the passwords
currently are — two bcrypt verifies per `up`, and a hash only when one is new or
different.

Three rules it holds to:

- **The users file is derived**, and is rebuilt whenever it disagrees with the
  passwords. Deleting it is recoverable.
- **Nothing is rotated.** An existing password is the input, not something to
  replace: a new one locks out every subscribed device. Hand-added accounts
  survive the rewrite.
- **A supplied password is never written to the secrets file.** Generated ones
  are, or they would exist nowhere.

## The uid

The image runs as **10001**. A Linux bind mount preserves host ownership; macOS
presents mounts as the container user and hides the difference.

- Anything writing to a bind mount needs `--user`.
- Anything reading a 600 file from one needs that file owned by 10001.

`bootstrap` runs as root so it can hand the secrets file over
(`CALSYNC_BOOTSTRAP_OWNER_UID`). Radicale's users file is written 0644 rather
than chowned, because that image's `UID`/`GID` environment can change which
account it runs as.

## Mounts

- **`./config/radicale` is the directory, not the files in it.** Docker creates
  a bind mount whose source does not exist as a *directory*.
- **`./secrets` is read-write for `web`, read-only for `calsync`.** Onboarding
  moves a feed's token out of the pasted URL and into the secret store.
- **`calsync-data` is read-write for the read-only API.** WAL needs `-wal` and
  `-shm` beside the database, and `open_db` migrates on startup.
- **`./config/calsync` is only for `calsync import`.** Configuration lives in
  the database.

## Healthchecks

- `api` is healthy on **401**, which proves the app serves *and* that the auth
  hook is in front of it.
- `web` fetches its own root, exercising the template layer.
- `proxy` has none, and no `depends_on` on what it fronts: Caddy resolves
  upstreams per request, so a service that is down is a 502 on its own path.
