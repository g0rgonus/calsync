#!/usr/bin/env bash
# Bring up the whole stack from a clean clone.
#
#   scripts/dev-stack.sh                 # config, secrets, containers
#   CALSYNC_ACCEPTANCE=1 .venv/bin/pytest tests/test_acceptance.py
#
# Development only. It writes generated secrets to ./.env, which is gitignored.
# A real deployment fills in its own.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p config/radicale config/caddy
cp deploy/radicale/config deploy/radicale/rights deploy/radicale/rights.anonymous config/radicale/
cp deploy/caddy/Caddyfile config/caddy/
echo "  wrote config/radicale/{config,rights,rights.anonymous} and config/caddy/Caddyfile"

# The three secrets the stack will not start without, in .env, which is where a
# real deployment puts them too. Radicale derives its users file from these on
# every start, so there is no second copy to keep in step.
#
# Generated in Python rather than `tr -dc </dev/urandom | head -c 32`, which
# looks fine and is a trap: head closes the pipe, tr dies of SIGPIPE, and under
# `set -e -o pipefail` the whole script exits having written half a file.
python3 - <<'INNER'
import pathlib, secrets

path = pathlib.Path(".env")
values = {}
if path.exists():
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip()

wanted = ("CALSYNC_SECRET_RADICALE_PASSWORD",
          "CALSYNC_SECRET_RADICALE_READER_PASSWORD",
          "CALSYNC_SECRET_API_TOKEN")
# Never regenerate one that is already set: the reader password is on every
# subscribed device, and a fresh one locks all of them out.
minted = [k for k in wanted if not values.get(k)]
for key in minted:
    values[key] = secrets.token_urlsafe(24)

path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
path.chmod(0o600)
print(f"  generated {len(minted)} secret(s) into .env" if minted
      else "  .env already holds all three secrets")
INNER

# --wait, because the next command talks to Radicale. `up -d` returns once
# containers are started, not once they serve, and nothing here depends_on the
# calendar — the poller does, but this script deliberately does not run it.
docker compose --profile demo up -d --wait radicale web proxy api feeds

# Compose seeds this on a fresh database (CALSYNC_SETTING_RADICALE_URL), so on a
# clean run the line below changes nothing. It stays because this script is the
# one caller that deliberately reuses an existing volume: seeding is first-run
# only, and a dev database created before that existed still says
# http://localhost:5232 — right when calsync runs on this machine and wrong
# inside every container, where localhost is the container itself.
docker compose run --rm --no-deps calsync set radicale_url http://radicale:5232 >/dev/null

# No -e here any more: the password reaches the container through env_file, the
# same path a deployment uses.
docker compose run --rm --no-deps calsync check || {
  echo "  the stack is up but calsync cannot reach it — see above" >&2
  exit 1
}
echo
echo "  console   http://localhost:8730/"
echo "  radicale  http://localhost:8730/cal/"
echo "  api       http://localhost:8730/v1"
echo "  feeds     http://localhost:8000"
