"""Command line entry point.

Scope is deliberately narrow: this drives the sync loop and nothing else. The
only wired target is ``ics_file`` via ``--out``, because that is the one that can
be pointed at a real feed without risking a calendar the family reads — the
output is inspectable, diffable, and committable. CalDAV needs a real HTTP
transport and a server to test against; until then it is reachable from code but
not from here, and says so rather than half-working.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import db, repo
from .secrets import SecretStore
from .sync import sync_all
from .targets import build


def _now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    stamp = datetime.fromisoformat(value)
    # A naive --now would make window filtering silently wrong by up to a day.
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def cmd_init_db(args) -> int:
    db.open_db(args.db)
    print(f"schema v{db.SCHEMA_VERSION} ready at {args.db}")
    return 0


def cmd_import(args) -> int:
    conn = db.open_db(args.db)
    data = config_mod.load(args.config)
    for line in config_mod.apply(conn, data):
        print(f"  {line}")
    print(f"imported {args.config} into {args.db}")
    return 0


def cmd_sync(args) -> int:
    conn = db.open_db(args.db)

    if args.out:
        target = build("ics_file", directory=args.out)
    else:
        print(
            "error: --out is required. Only the ics_file target is wired to the "
            "CLI; CalDAV has no HTTP transport yet.",
            file=sys.stderr,
        )
        return 2

    raw = None
    if args.from_file:
        raw = Path(args.from_file).read_bytes()
        if not args.source:
            print("error: --from-file needs --source to say which source it is",
                  file=sys.stderr)
            return 2

    secrets = SecretStore(path=args.secrets) if args.secrets else SecretStore()
    reports = []
    for source in repo.list_sources(conn, enabled_only=True):
        if args.source and source.id != args.source:
            continue
        from .sync import sync_source

        reports.append(
            sync_source(
                conn, source, target, now=_now(args.now), secrets=secrets,
                raw=raw, dry_run=args.dry_run,
            )
        )

    if not reports:
        print("no enabled sources matched — run `calsync import <config.yaml>` first")
        return 1

    exit_code = 0
    for report in reports:
        print(report.line())
        if report.status == "error":
            exit_code = 1
        elif report.status == "held":
            # Held is not failure: the guard did its job. Distinct code so a
            # scheduler can alert on it without treating it as a crash.
            exit_code = max(exit_code, 3)
    if args.dry_run:
        print("(dry run — nothing was written)")
    return exit_code


def cmd_status(args) -> int:
    conn = db.open_db(args.db)
    sources = repo.list_sources(conn, enabled_only=False)
    if not sources:
        print("no sources configured")
        return 0
    for source in sources:
        row = conn.execute(
            "SELECT last_success_at, last_error, last_error_at FROM sources WHERE id = ?",
            (source.id,),
        ).fetchone()
        tracked = conn.execute(
            "SELECT COUNT(*) AS n FROM event_state WHERE source_id = ? AND cancelled = 0",
            (source.id,),
        ).fetchone()["n"]
        flag = "" if source.enabled else " (disabled)"
        print(f"{source.id}{flag}: {tracked} tracked events, last ok {row['last_success_at'] or 'never'}")
        if row["last_error"]:
            print(f"  last error {row['last_error_at']}: {row['last_error']}")
        for run in conn.execute(
            "SELECT started_at, status, detail FROM poll_runs WHERE source_id = ? "
            "ORDER BY id DESC LIMIT 3",
            (source.id,),
        ):
            print(f"  {run['started_at']}  {run['status']:5}  {run['detail'] or ''}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calsync", description=__doc__)
    parser.add_argument("--db", default="calsync.db", help="SQLite path (default: calsync.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or migrate the database").set_defaults(fn=cmd_init_db)

    p_import = sub.add_parser("import", help="load a YAML config into the database")
    p_import.add_argument("config")
    p_import.set_defaults(fn=cmd_import)

    p_sync = sub.add_parser("sync", help="poll sources and write events")
    p_sync.add_argument("--out", help="write .ics files under this directory")
    p_sync.add_argument("--source", help="only this source id")
    p_sync.add_argument("--from-file", help="replay a saved payload instead of fetching")
    p_sync.add_argument("--secrets", help="path to a secrets JSON file")
    p_sync.add_argument("--now", help="ISO timestamp to treat as now (for reproducible runs)")
    p_sync.add_argument("--dry-run", action="store_true", help="report the diff, write nothing")
    p_sync.set_defaults(fn=cmd_sync)

    sub.add_parser("status", help="show per-source health").set_defaults(fn=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
