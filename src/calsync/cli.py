"""Command line entry point.

Two ways to run a sync:

- ``--out DIR`` writes a directory of ``.ics`` files. Safe to point at a live
  feed, inspectable, and it diffs in git — the right thing while working out a
  new feed's parsing.
- otherwise the CalDAV target configured in settings, which is what the compose
  stack runs against Radicale.

``poll`` is the long-running form for the container; ``sync`` is one pass.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import db, repo
from .secrets import SecretStore
from .settings import Settings
from . import retire
from .sync import sync_source
from .targets import build
from .targets.http import HttpTransport

#: How often the poll loop wakes to see what is due. Sources declare their own
#: interval; this is only the granularity of the check.
TICK_S = 30


def _now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    stamp = datetime.fromisoformat(value)
    # A naive --now would make window filtering silently wrong by up to a day.
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _secrets(args) -> SecretStore:
    return SecretStore(path=args.secrets) if getattr(args, "secrets", None) else SecretStore()


def _target(conn, args, secrets: SecretStore):
    """ics_file when --out is given, else the configured CalDAV server."""
    if getattr(args, "out", None):
        return build("ics_file", directory=args.out)

    settings = Settings.load(conn)
    password = secrets.get(settings.radicale_secret_ref)
    return build(
        "caldav",
        base_url=settings.radicale_url,
        transport=HttpTransport(username=settings.radicale_user, password=password),
        username=settings.radicale_user,
        password=password,
    )


def _emit(report) -> None:
    print(report.line())
    for line in report.diagnostic_lines():
        print(f"    ? {line}")


def cmd_init_db(args) -> int:
    db.open_db(args.db)
    print(f"schema v{db.SCHEMA_VERSION} ready at {args.db}")
    return 0


def cmd_import(args) -> int:
    conn = db.open_db(args.db)
    for line in config_mod.apply(conn, config_mod.load(args.config)):
        print(f"  {line}")
    print(f"imported {args.config} into {args.db}")
    return 0


def _run_once(conn, args, secrets, *, only=None, raw=None, dry_run=False):
    target = _target(conn, args, secrets)
    reports = []
    for source in repo.list_sources(conn, enabled_only=True):
        if only and source.id != only:
            continue
        reports.append(
            sync_source(
                conn, source, target, now=_now(getattr(args, "now", None)),
                secrets=secrets, raw=raw, dry_run=dry_run,
            )
        )
    return reports


def cmd_sync(args) -> int:
    conn = db.open_db(args.db)

    raw = None
    if args.from_file:
        if not args.source:
            print("error: --from-file needs --source to say which source it is",
                  file=sys.stderr)
            return 2
        raw = Path(args.from_file).read_bytes()

    reports = _run_once(conn, args, _secrets(args), only=args.source, raw=raw,
                        dry_run=args.dry_run)
    if not reports:
        print("no enabled sources matched — run `calsync import <config.yaml>` first")
        return 1

    exit_code = 0
    for report in reports:
        _emit(report)
        if report.status == "error":
            exit_code = 1
        elif report.status == "held":
            # Held is not failure: the guard did its job. A distinct code lets a
            # scheduler alert on it without treating it as a crash.
            exit_code = max(exit_code, 3)
    if args.dry_run:
        print("(dry run — nothing was written)")
    return exit_code


def cmd_poll(args) -> int:
    """Long-running loop for the container.

    Each source declares its own ``poll_interval_s``; this wakes on a fixed tick
    and syncs whatever is due. Due-times are in memory, so a restart polls
    everything once — harmless, and cheaper than persisting a schedule.
    """
    conn = db.open_db(args.db)
    secrets = _secrets(args)
    stopping = {"now": False}

    def stop(signum, _frame):
        # Finish the source in flight rather than dying mid-write: a killed
        # process between a target write and its state row is the one ordering
        # the sync loop cannot defend against.
        print(f"signal {signum}, finishing current source then exiting", flush=True)
        stopping["now"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    due: dict[str, float] = {}
    while not stopping["now"]:
        clock = time.monotonic()
        for source in repo.list_sources(conn, enabled_only=True):
            if stopping["now"]:
                break
            if clock < due.get(source.id, 0.0):
                continue
            target = _target(conn, args, secrets)
            report = sync_source(
                conn, source, target, now=_now(None), secrets=secrets
            )
            _emit(report)
            sys.stdout.flush()
            due[source.id] = time.monotonic() + max(source.poll_interval_s, 60)

        if args.once:
            return 0
        for _ in range(TICK_S):
            if stopping["now"]:
                break
            time.sleep(1)
    return 0


def cmd_stage(args) -> int:
    conn = db.open_db(args.db)
    if repo.get_source(conn, args.source) is None:
        print(f"error: no source {args.source!r}", file=sys.stderr)
        return 2
    repo.set_staging(conn, args.source, args.collection)
    print(f"{args.source} now staging to {args.collection!r}")
    print("subscribe to that collection on your phone, then `calsync promote` when it looks right")
    return 0


def cmd_promote(args) -> int:
    """Move a source off the onboarding calendar, gated on a clean parse.

    The gate runs a real dry-run rather than trusting a stored verdict, so it
    reflects the feed as it is now — including fixtures that only appeared after
    onboarding.
    """
    conn = db.open_db(args.db)
    source = repo.get_source(conn, args.source)
    if source is None:
        print(f"error: no source {args.source!r}", file=sys.stderr)
        return 2
    if not source.staging_collection:
        print(f"{args.source} is not staged; nothing to promote")
        return 0

    reports = _run_once(conn, args, _secrets(args), only=args.source, dry_run=True)
    if not reports:
        print(f"error: {args.source} is disabled", file=sys.stderr)
        return 2
    report = reports[0]
    _emit(report)

    if not report.promotable and not args.force:
        if report.fixtures_seen == 0:
            print("\nnot promoting: no fixtures in the feed yet, so the opponent and")
            print("home/away parsing has never run. Wait for the game schedule, or --force.")
        else:
            print("\nnot promoting: the parse still has gaps (listed above).")
            print("Fix them — usually an activity alias or a venue row — or --force.")
        return 3

    repo.set_staging(conn, args.source, None)
    print(f"\npromoted {args.source}; the next sync moves its events out of "
          f"{source.staging_collection!r}")
    return 0


def cmd_retire(args) -> int:
    """Take a finished season off the calendars and stop polling it.

    Not a delete: the source row and its tombstones stay, because they are the
    record that these events were ours. `--forget` drops the row afterwards,
    and refuses while anything is still live.
    """
    conn = db.open_db(args.db)
    source = repo.get_source(conn, args.source)
    if source is None:
        print(f"error: no source {args.source!r}", file=sys.stderr)
        return 2

    secrets = _secrets(args)
    report = retire.retire_source(conn, source, _target(conn, args, secrets))
    print(report.line())
    if not report.ok:
        print("\nnothing was disabled: some events could not be removed, so the",
              file=sys.stderr)
        print("source stays enabled and a later run will retry them.", file=sys.stderr)
        return 1

    if args.forget:
        retire.forget_source(conn, args.source)
        print(f"forgot {args.source}; its row is gone")
    return 0


def cmd_web(args) -> int:
    """Serve the onboarding console.

    Loopback by default and it should stay that way — this serves children's
    names, schedules and the places they will be. Reach it from a phone through
    whatever VPN or authenticating proxy already fronts the homelab; the console
    has no login of its own.
    """
    from .web import create_app, serve

    serve(
        create_app(
            args.db, secrets=_secrets(args), trusted_origins=args.trusted_origin or ()
        ),
        host=args.host,
        port=args.port,
    )
    return 0


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
        flags = []
        if not source.enabled:
            flags.append("disabled")
        if source.staging_collection:
            flags.append(f"staging→{source.staging_collection}")
        suffix = f" ({', '.join(flags)})" if flags else ""
        print(f"{source.id}{suffix}: {tracked} tracked events, "
              f"last ok {row['last_success_at'] or 'never'}")
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

    def target_args(p):
        p.add_argument("--out", help="write .ics files here instead of to CalDAV")
        p.add_argument("--secrets", help="path to a secrets JSON file")

    p_sync = sub.add_parser("sync", help="poll sources once and write events")
    target_args(p_sync)
    p_sync.add_argument("--source", help="only this source id")
    p_sync.add_argument("--from-file", help="replay a saved payload instead of fetching")
    p_sync.add_argument("--now", help="ISO timestamp to treat as now (reproducible runs)")
    p_sync.add_argument("--dry-run", action="store_true", help="report the diff, write nothing")
    p_sync.set_defaults(fn=cmd_sync)

    p_poll = sub.add_parser("poll", help="run continuously, honouring each source's interval")
    target_args(p_poll)
    p_poll.add_argument("--once", action="store_true", help="one pass, then exit")
    p_poll.set_defaults(fn=cmd_poll)

    p_stage = sub.add_parser("stage", help="route a source to an onboarding collection")
    p_stage.add_argument("source")
    p_stage.add_argument("--collection", default="onboarding")
    p_stage.set_defaults(fn=cmd_stage)

    p_promote = sub.add_parser("promote", help="move a source off the onboarding collection")
    target_args(p_promote)
    p_promote.add_argument("source")
    p_promote.add_argument("--now")
    p_promote.add_argument("--force", action="store_true",
                           help="promote despite an incomplete parse")
    p_promote.set_defaults(fn=cmd_promote)

    p_retire = sub.add_parser(
        "retire", help="cancel a finished season's events and stop polling it")
    target_args(p_retire)
    p_retire.add_argument("source")
    p_retire.add_argument("--forget", action="store_true",
                          help="also drop the source row, once nothing is left live")
    p_retire.set_defaults(fn=cmd_retire)

    p_web = sub.add_parser("web", help="serve the onboarding console")
    p_web.add_argument("--host", default="127.0.0.1",
                       help="bind address (default: loopback; use tailscale serve)")
    p_web.add_argument("--port", type=int, default=8730)
    p_web.add_argument("--secrets", help="path to a secrets JSON file")
    p_web.add_argument(
        "--trusted-origin", action="append", metavar="HOST[:PORT]",
        help="accept writes from this origin as well as the served host; only "
             "needed behind a proxy that rewrites Host and for browsers too old "
             "to send Sec-Fetch-Site",
    )
    p_web.set_defaults(fn=cmd_web)

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
