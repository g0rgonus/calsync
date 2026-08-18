#!/usr/bin/env bash
# Back up everything that cannot be rebuilt.
#
#   scripts/backup.sh [DEST]        # default ./backups
#
# **Radicale's data is the irreplaceable half, not the database.** That is the
# opposite of the intuition, so it is worth stating: calsync's SQLite file keeps
# event *content* only for `sync_window_back_days` (7 by default) and prunes the
# rest, and the feeds themselves drop a season within months of it ending. So
# every game the kids actually played exists in exactly one place — the calendar
# server — and it is the one thing here that no amount of re-polling can
# reconstruct. `retire.py` goes out of its way not to delete those events; a
# backup that skipped Radicale would delete them anyway, just more slowly.
#
# The database and the secrets are painful to lose rather than impossible: they
# are every venue alias, every onboarding decision, and the record of which
# events are ours. Without them a restore means re-onboarding every team by hand
# and orphaning everything already on the family's calendars.
#
# Run it from cron on the host, daily, before anything else touches the disk:
#
#   17 4 * * *  cd /srv/calsync && scripts/backup.sh /srv/backups >> /var/log/calsync-backup.log 2>&1
#
# Development and homelab scale: whole-file copies, no incrementals, no rotation
# beyond what you delete yourself. At a few hundred events a season that is
# kilobytes, and a scheme nobody can restore from by hand is worse than none.
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="${1:-./backups}"
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
OUT="$DEST/calsync-$STAMP"

# Compose derives volume names from the project, which defaults to the
# directory name. Overridable the same way compose itself takes it.
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}"
DB_VOLUME="${PROJECT}_calsync-data"
CAL_VOLUME="${PROJECT}_radicale-data"

command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }
for volume in "$DB_VOLUME" "$CAL_VOLUME"; do
  docker volume inspect "$volume" >/dev/null 2>&1 || {
    echo "no docker volume $volume — is this the right directory, or is" >&2
    echo "COMPOSE_PROJECT_NAME set to something else?" >&2
    exit 1
  }
done

mkdir -p "$OUT"
# Before anything lands in it: this ends up holding bearer tokens for feeds that
# serve children's schedules, and the window between a world-readable directory
# and a chmod is exactly long enough to matter.
chmod 700 "$DEST" "$OUT"

echo "backing up to $OUT"

# --- the calendar, first because it is the copy that cannot be rebuilt -------
docker run --rm \
  -v "$CAL_VOLUME:/source:ro" \
  -v "$(cd "$OUT" && pwd):/backup" \
  python:3.12-slim \
  tar czf /backup/radicale-data.tar.gz -C /source .
# Written by a root container with its own umask; this holds every event on the
# family's calendars, so it does not stay world-readable.
chmod 600 "$OUT/radicale-data.tar.gz"
echo "  radicale-data.tar.gz  $(du -h "$OUT/radicale-data.tar.gz" | cut -f1)"

# --- the database -----------------------------------------------------------
#
# `sqlite3.Connection.backup()`, never `cp`. The poller and the console are live
# writers on this file in WAL mode, so a plain copy can catch it mid-transaction
# and produce something that opens fine and is subtly torn — and the -wal file
# alongside it is part of the state, so copying the .db alone loses whatever has
# not been checkpointed. The online backup API takes a consistent snapshot of a
# database being written to, which is the whole reason it exists.
#
# Then `PRAGMA integrity_check` on the *result*. An unverified backup is a
# belief, not a backup, and this is the cheapest possible moment to find out.
# `-i` is load-bearing: `python -` reads the program from stdin, and without it
# docker attaches no stdin, python reads EOF, does nothing and exits 0 — a
# backup with no database in it that reports success. Found by running this.
docker run --rm -i \
  -v "$DB_VOLUME:/source" \
  -v "$(cd "$OUT" && pwd):/backup" \
  python:3.12-slim \
  python - <<'PY'
import sqlite3, sys

source = sqlite3.connect("/source/calsync.db")
target = sqlite3.connect("/backup/calsync.db")
with target:
    source.backup(target)
source.close()

verdict = target.execute("PRAGMA integrity_check").fetchone()[0]
counts = {
    table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    for table in ("sources", "activities", "children", "venues",
                  "venue_aliases", "event_state")
}
target.close()

if verdict != "ok":
    print(f"  calsync.db  INTEGRITY CHECK FAILED: {verdict}", file=sys.stderr)
    sys.exit(1)
print("  calsync.db  integrity ok — " + ", ".join(
    f"{n} {t}" for t, n in counts.items()))
PY

# --- the credentials --------------------------------------------------------
#
# Without these the database is a set of `{{secret:ref}}` templates and no
# tokens, so a restore means finding every feed URL again. Backed up for that
# reason and kept 600 for the obvious one.
if [ -f secrets/secrets.json ]; then
  install -m 600 secrets/secrets.json "$OUT/secrets.json"
  echo "  secrets.json  $(python3 -c 'import json;print(len(json.load(open("secrets/secrets.json"))),"refs")')"
else
  echo "  secrets.json  (none found — nothing to back up)" >&2
fi

# Radicale's own htpasswd and rights files are configuration rather than data,
# but they are hand-made and small, and a restore without them is a calendar
# server nobody can log in to.
if [ -d config/radicale ]; then
  tar czf "$OUT/radicale-config.tar.gz" -C config/radicale .
  chmod 600 "$OUT/radicale-config.tar.gz"
  echo "  radicale-config.tar.gz"
fi

cat > "$OUT/RESTORE.md" <<'DOC'
# Restoring this backup

Taken by `scripts/backup.sh`. Restore into a stopped stack, in this order.

```bash
docker compose down
```

## 1. The calendar — do this one first

This is the only copy of every season that has already been played. calsync's
database keeps event *content* for a week and prunes the rest, and the team
feeds drop a season within months of it ending, so nothing else can reconstruct
these events.

```bash
docker volume create calsync_radicale-data
docker run --rm -v calsync_radicale-data:/target -v "$PWD:/backup" \
  python:3.12-slim tar xzf /backup/radicale-data.tar.gz -C /target
```

## 2. The database

```bash
docker volume create calsync_calsync-data
docker run --rm -v calsync_calsync-data:/target -v "$PWD:/backup" \
  python:3.12-slim cp /backup/calsync.db /target/calsync.db
```

A plain `cp` is correct *here* — the backup is a quiescent file, unlike the
live database it came from, which is why taking it needed the online backup API
and restoring it does not.

## 3. Credentials and server config

```bash
mkdir -p secrets config/radicale
install -m 600 secrets.json secrets/secrets.json
chmod 700 secrets
tar xzf radicale-config.tar.gz -C config/radicale
```

## 4. Start, and check it agrees with itself

```bash
docker compose up -d
docker compose run --rm calsync status
```

`status` lists every source with its last success. Then run one sync and expect
it to report almost entirely `unchanged` — a restored database that reports the
whole season as `new` has lost its `event_state` and is about to write a second
copy of every event into the family's calendars.

```bash
docker compose run --rm calsync sync --dry-run
```

Nothing is written by a dry run, so this is safe to check before committing to
it.

## What this backup does not contain

The `.venv`, the source tree, and anything in `config/calsync/` used only for
`calsync import`. All of those come from git.
DOC
chmod 600 "$OUT/RESTORE.md"

# --- verify, because a step exiting 0 is not evidence it did anything --------
#
# The first version of this script lost the database entirely and reported
# success: a `docker run` with no stdin ran `python -` against EOF, which is a
# no-op with a zero exit code. Checking exit codes would not have caught it.
# Checking that the file exists and is a database does.
for required in calsync.db radicale-data.tar.gz; do
  [ -s "$OUT/$required" ] || {
    echo "BACKUP INCOMPLETE: $required is missing or empty" >&2
    exit 1
  }
done
chmod 600 "$OUT/calsync.db"
head -c 15 "$OUT/calsync.db" | grep -q "SQLite format 3" || {
  echo "BACKUP INCOMPLETE: calsync.db is not a SQLite database" >&2
  exit 1
}
tar tzf "$OUT/radicale-data.tar.gz" >/dev/null || {
  echo "BACKUP INCOMPLETE: radicale-data.tar.gz will not list" >&2
  exit 1
}

echo
echo "  RESTORE.md            how to put it back"
echo "done: $OUT"
