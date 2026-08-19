#!/usr/bin/env bash
# Bring up the whole stack from a clean clone.
#
# The README documents this as a copy, two htpasswd invocations and a hand-written
# secrets file. That is four chances to get it subtly wrong, and it is the reason
# nothing has ever tested calsync against a real Radicale — the setup cost more
# than the test was worth. Doing it here makes the acceptance suite runnable:
#
#   scripts/dev-stack.sh                 # config, credentials, containers
#   CALSYNC_ACCEPTANCE=1 .venv/bin/pytest tests/test_acceptance.py
#
# Development only. It writes a generated password to ./secrets/secrets.json,
# which is gitignored. A real deployment sets its own.
set -euo pipefail
cd "$(dirname "$0")/.."

RADICALE_USER="${RADICALE_USER:-calsync}"
READER_USER="${READER_USER:-calreader}"

command -v htpasswd >/dev/null || {
  echo "htpasswd not found (apache2-utils / httpd-tools)" >&2; exit 1; }

mkdir -p config/radicale config/caddy secrets
cp deploy/radicale/config deploy/radicale/rights config/radicale/
# The stack publishes one port and `proxy` serves it. `bootstrap` places this
# too, on a real deployment; here the script lays out config/ itself.
cp deploy/caddy/Caddyfile config/caddy/

# Generated, not prompted: an interactive password in a setup script is a
# password that ends up in shell history or in a doc as a literal.
#
# Generated in Python rather than `tr -dc </dev/urandom | head -c 32`, which
# looks fine and is a trap: head closes the pipe, tr dies of SIGPIPE, and under
# `set -e -o pipefail` the whole script exits right here having written half a
# configuration. It also gives us a CSPRNG rather than a shell pipeline.
python3 <<'PY'
import json, os, pathlib, secrets, string
path = pathlib.Path("secrets/secrets.json")
data = json.loads(path.read_text()) if path.exists() else {}
if not data.get("radicale_password"):
    alphabet = string.ascii_letters + string.digits
    data["radicale_password"] = "".join(secrets.choice(alphabet) for _ in range(32))
    print("  generated a Radicale password into secrets/secrets.json")
# Both refs, with one value, because the htpasswd calls below give both
# accounts the same password. The stack's `bootstrap` service derives the users
# file from what the store holds: leave the reader's out and it finds a missing
# credential, mints one, and rewrites the file this script just built — which
# invalidates the calreader entry the R8 acceptance checks authenticate with.
data.setdefault("radicale_reader_password", data["radicale_password"])
# Same reasoning for the API's token. Any secret bootstrap finds missing it
# mints, and `SecretStore.put` writes a whole new file — as root, mode 600,
# inside a container. The host then cannot read the passwords the acceptance
# suite authenticates with. Seeding every secret bootstrap knows about is what
# keeps this file the host's.
data.setdefault("api_token", secrets.token_urlsafe(32))
# Mode before content: the gap between creating a world-readable file and
# chmod-ing it is exactly long enough to lose a credential.
fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
chmod 700 secrets

password="$(python3 -c 'import json;print(json.load(open("secrets/secrets.json"))["radicale_password"])')"

# bcrypt, per deploy/radicale/config — plaintext htpasswd would be accepted by
# the server and silently defeat the point.
htpasswd -bBc config/radicale/users "$RADICALE_USER" "$password" 2>/dev/null
htpasswd -bB  config/radicale/users "$READER_USER"  "$password" 2>/dev/null
echo "  wrote config/radicale/{config,rights,users} and config/caddy/Caddyfile"

# Keep the credentials owned by whoever ran this. `bootstrap` hands the secrets
# file to uid 10001 on a real deployment, because a Linux bind mount preserves
# host ownership and the poller could not read it otherwise — but here the
# acceptance tests read that same file from the host, as this user, and a 600
# file owned by 10001 is one they cannot open.
export CALSYNC_BOOTSTRAP_OWNER_UID=0

docker compose --profile demo up -d radicale web proxy feeds

# Compose seeds this on a fresh database (CALSYNC_SETTING_RADICALE_URL), so on
# a clean run the line below changes nothing. It stays because this script is
# the one caller that deliberately reuses an existing volume: seeding is
# first-run only, and a dev database created before that existed still says
# http://localhost:5232 — right when calsync runs on this machine and wrong
# inside every container, where localhost is the container itself. Setting it
# explicitly makes the script correct from either starting point.
docker compose run --rm --no-deps calsync set radicale_url http://radicale:5232 \
  >/dev/null

# The password goes in by environment rather than through the mounted file, and
# not for convenience. The container runs as uid 10001 and `SecretStore` refuses
# a secrets file any other account can read — so on Linux, where a bind mount
# keeps host ownership, the file is either readable by the container or by the
# tests running on this machine, and never by both. `SecretStore` checks the
# environment first, which sidesteps the question entirely for this one call.
#
# The long-running poller still reads the mounted file, and on a Linux host that
# file has to be owned by uid 10001. See docker-compose.yml.
docker compose run --rm --no-deps \
  -e CALSYNC_SECRET_RADICALE_PASSWORD="$password" calsync check || {
  echo "  the stack is up but calsync cannot reach it — see above" >&2
  exit 1
}
echo
# One published port, routed by path — docs/deployment/proxy.md. Radicale is
# no longer on 5232 from the host, which is what the acceptance suite's
# default base URL follows.
echo "  console   http://localhost:8730/"
echo "  radicale  http://localhost:8730/cal/"
echo "  feeds     http://localhost:8000"
