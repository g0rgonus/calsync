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
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import db, repo
from .secrets import SecretError, SecretStore
from .settings import Settings, set_setting
from . import digest, enrichment, matrix, polling, retire, seasonend, targeting
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
    """Whatever this deployment is configured to write to.

    One selection point, shared with the console, so a `--target` here and the
    console's idea of where events go cannot drift apart.
    """
    return targeting.build_target(
        conn,
        kind=getattr(args, "target", None),
        out_dir=getattr(args, "out", None),
        secrets=secrets,
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


def _startup_check(conn, args, secrets) -> bool:
    """Ask once, at boot, whether the configured calendar is really there.

    A first run that cannot reach its target must not start. The failure this
    guards against is not a crash — it is a stack that comes up healthy, writes
    nothing, and reports it once per event before backing off to three-hourly,
    which is how a real deployment went unnoticed for days. Refusing to start
    turns that into a container that will not come up, with the reason on the
    first line of its log.

    A deployment that has polled before only gets a warning. By then the
    address is known to have worked, so a failure here is far more likely to be
    Radicale still starting or briefly down than a misconfiguration — and
    exiting would take the poller off the air for an outage the sync loop
    already handles with backoff.
    """
    # `--out` and `--target ics_file` write files, so there is no server to
    # ask and the configured `target_kind` is not what this run will use.
    if getattr(args, "out", None) or getattr(args, "target", None) == "ics_file":
        return True

    check = targeting.verify(conn, secrets)
    if check.ok:
        return True

    polled_before = conn.execute(
        "SELECT 1 FROM poll_runs WHERE status = 'ok' LIMIT 1"
    ).fetchone() is not None
    for finding in check.findings:
        print(f"  {'ok ' if finding.ok else 'NO '} {finding.label}: {finding.detail}",
              file=sys.stderr, flush=True)
    if polled_before:
        print("calendar unreachable at startup; polling anyway, since this "
              "deployment has synced before", file=sys.stderr, flush=True)
        return True
    print("refusing to start: this database has never synced and the calendar "
          "cannot be reached. Fix the settings above (`calsync check` asks the "
          "same question) — a poller left running like this writes nothing and "
          "says so only in passing.", file=sys.stderr, flush=True)
    return False


def cmd_poll(args) -> int:
    """Long-running loop for the container.

    Each source declares its own ``poll_interval_s``; this wakes on a fixed tick
    and syncs whatever is due. Due-times are in memory, so a restart polls
    everything once — harmless, and cheaper than persisting a schedule.
    """
    conn = db.open_db(args.db)
    secrets = _secrets(args)
    if not _startup_check(conn, args, secrets):
        return 1
    stopping = {"now": False}

    def stop(signum, _frame):
        # Finish the source in flight rather than dying mid-write: a killed
        # process between a target write and its state row is the one ordering
        # the sync loop cannot defend against.
        print(f"signal {signum}, finishing current source then exiting", flush=True)
        stopping["now"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    schedule = polling.Schedule()
    # In memory rather than a row: the Matrix transaction id is the date, so a
    # restart that re-sends updates the same message instead of posting twice.
    # Persisting this would buy nothing and be one more thing to migrate.
    digest_sent_on = None

    while not stopping["now"]:
        clock = time.monotonic()
        live = repo.list_sources(conn, enabled_only=True)
        for gone in set(schedule.due) - {s.id for s in live}:
            # Retired or paused since the last pass. Dropping its state stops a
            # stale backoff from applying to a source that reuses the id.
            schedule.forget(gone)

        for source in live:
            if stopping["now"]:
                break
            if not schedule.is_due(source.id, clock):
                continue
            target = _target(conn, args, secrets)
            report = sync_source(
                conn, source, target, now=_now(None), secrets=secrets
            )
            _emit(report)
            delay = schedule.record(
                source.id, status=report.status,
                interval_s=source.poll_interval_s, now=time.monotonic(),
            )
            # After the sync, never inside it: this can disable a source, and
            # the sync loop's ordering is not somewhere to add a side effect.
            try:
                outcome = seasonend.review(
                    conn, source, now=_now(None), secrets=secrets,
                    base_url=args.console_url or "",
                )
                if outcome.disabled:
                    print("    season looks finished; polling stopped", flush=True)
                elif outcome.notified:
                    print("    season looks finished; notified", flush=True)
                for problem in outcome.errors:
                    print(f"    could not notify: {problem}", flush=True)
            except Exception as exc:  # noqa: BLE001 — never take the poller down
                print(f"    season check failed: {exc}", flush=True)

            # Also after the sync, and for the same reason: this reads what the
            # poll actually wrote to the enrichment collection, so it has to run
            # once that is true.
            try:
                waiting = enrichment.review(
                    conn, source, report, secrets=secrets,
                    base_url=args.console_url or "",
                )
                if waiting.notified:
                    print(f"    {waiting.held} event(s) awaiting review; notified",
                          flush=True)
                elif waiting.held:
                    print(f"    {waiting.held} event(s) awaiting review", flush=True)
                for problem in waiting.errors:
                    print(f"    could not notify: {problem}", flush=True)
            except Exception as exc:  # noqa: BLE001 — never take the poller down
                print(f"    review check failed: {exc}", flush=True)

            # And the same questions to the room, for whatever is listening.
            # Separate from the push above: different audience, and a room that
            # is not configured must not stop the poller or the notification.
            try:
                posted = enrichment.dispatch(conn, source, report, secrets=secrets)
                if posted.notified:
                    print("    open questions posted to the room", flush=True)
                for problem in posted.errors:
                    print(f"    could not post to the room: {problem}", flush=True)
            except Exception as exc:  # noqa: BLE001 — never take the poller down
                print(f"    task dispatch failed: {exc}", flush=True)

            failures = schedule.struggling().get(source.id, 0)
            if failures > 1:
                print(f"    backing off: {failures} failures in a row, "
                      f"next attempt in {delay // 60}m", flush=True)
            sys.stdout.flush()

        digest_sent_on = _maybe_digest(conn, secrets, digest_sent_on)

        if args.once:
            return 0
        for _ in range(TICK_S):
            if stopping["now"]:
                break
            time.sleep(1)
    return 0


def _maybe_digest(conn, secrets, sent_on):
    """Send the daily digest if it is due. Never takes the poller down.

    Lives in the poll loop because that is already a long-running process with
    the database and the secret store to hand — a cron entry or a second
    container would be another place for credentials to live and another thing
    to notice had stopped.
    """
    from zoneinfo import ZoneInfo

    try:
        settings = Settings.load(conn)
        if not settings.digest_send_at.strip():
            return sent_on
        now_local = datetime.now(ZoneInfo(settings.default_tz))
        if not digest.due(now_local=now_local, send_at=settings.digest_send_at,
                          last_sent_on=sent_on):
            return sent_on

        now = _now(None)
        result = digest.collect(conn, now=now, hours=settings.digest_window_hours)
        if result.empty and not result.stale:
            print("digest: nothing on, not sending", flush=True)
            return now_local.date()

        matrix.send(matrix.load(conn), secrets, result.text(),
                    transaction_id=f"calsync-digest-{now_local.date().isoformat()}")
        print(f"digest: sent for {now_local.date()}", flush=True)
        return now_local.date()
    except Exception as exc:  # noqa: BLE001 — a digest is not worth a crash
        print(f"digest: not sent ({exc})", flush=True)
        # Marked as done for today anyway: retrying a broken send every thirty
        # seconds until midnight is worse than missing one message.
        return datetime.now(timezone.utc).date()


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
    """Stop polling a source and clear what it still has coming.

    Events that have already happened stay on the calendar — a finished season
    is a record of games that were played, not clutter (see `retire.py`). So a
    season retired a month after it ended usually removes nothing at all, and
    says so.

    Not a delete either: the source row and its state rows stay, because they are
    the record that these events were ours. `--forget` drops the row afterwards,
    and refuses while anything is still to come.
    """
    conn = db.open_db(args.db)
    source = repo.get_source(conn, args.source)
    if source is None:
        print(f"error: no source {args.source!r}", file=sys.stderr)
        return 2

    secrets = _secrets(args)
    now = _now(getattr(args, "now", None))
    report = retire.retire_source(conn, source, _target(conn, args, secrets), now=now)
    print(report.line())
    if not report.ok:
        print("\nnothing was disabled: some events could not be removed, so the",
              file=sys.stderr)
        print("source stays enabled and a later run will retry them.", file=sys.stderr)
        return 1

    if args.forget:
        retire.forget_source(conn, args.source, now=now)
        print(f"forgot {args.source}; its row is gone")
    return 0


def cmd_digest(args) -> int:
    """What is on in the next day. Prints it; `--send` puts it in the room.

    Reads nothing back from the calendar and writes nothing anywhere — see
    `digest.py` for why both of those are deliberate.
    """
    conn = db.open_db(args.db)
    secrets = _secrets(args)
    now = _now(args.now)

    result = digest.collect(conn, now=now, hours=args.hours)
    print(result.text())

    if not args.send:
        return 0
    if result.empty and not args.empty_is_news and not result.stale:
        # A daily "nothing on" is how a room learns to ignore this.
        print("(nothing on — not sending)")
        return 0

    config = matrix.load(conn)
    try:
        # Derived from the day, so a retry or a double cron blaze updates rather
        # than posting the family's schedule twice.
        event_id = matrix.send(
            config, secrets, result.text(),
            transaction_id=f"calsync-digest-{now.date().isoformat()}",
        )
    except (matrix.MatrixError, SecretError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"sent to {config.room_id} ({event_id})")
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


#: Where the deployment assets live inside the image, and the same files in a
#: checkout. Both, so `init-deploy` works whether you pulled the image or cloned.
DEPLOY_ASSETS = (
    Path("/app/deploy-assets"),
    Path(__file__).resolve().parent.parent.parent,
)


def cmd_init_deploy(args) -> int:
    """Write out everything a deployment needs besides the image.

    A published image is only half of "you do not need the repo": compose still
    has to come from somewhere, and so does Radicale's configuration. Baking
    them into the image means one `docker run` lays out a stack, and the files
    match the image that produced them rather than whatever a branch has moved
    on to.

    Never overwrites. These are files somebody edits — the rights file in
    particular — and silently replacing an edited one during a routine upgrade
    is how a deployment loses a change nobody remembers making.
    """
    source = next((p for p in DEPLOY_ASSETS if (p / "docker-compose.yml").exists()), None)
    if source is None:
        print("error: this build carries no deployment assets", file=sys.stderr)
        return 1

    dest = Path(args.directory)
    wanted = [
        (source / "docker-compose.yml", dest / "docker-compose.yml"),
        # Not `.env`: this writes files nobody has edited yet, and a `.env`
        # holding real credentials is exactly the file a later `init-deploy`
        # must not be able to touch. The example is copied, and copying it is
        # the deployment's job.
        (source / ".env.example", dest / ".env.example"),
        (source / "deploy" / "radicale" / "config", dest / "config" / "radicale" / "config"),
        (source / "deploy" / "radicale" / "rights", dest / "config" / "radicale" / "rights"),
        (source / "deploy" / "caddy" / "Caddyfile", dest / "config" / "caddy" / "Caddyfile"),
    ]

    written, kept = [], []
    try:
        for src, out in wanted:
            if out.exists():
                kept.append(out)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(src.read_bytes())
            written.append(out)
    except PermissionError as exc:
        # The usual cause, and a bare EACCES sends people looking in the wrong
        # place. This image runs as uid 10001 and a Linux bind mount keeps host
        # ownership, so writing into a directory you own needs the container to
        # be you.
        print(f"error: cannot write to {dest}: {exc}", file=sys.stderr)
        print("", file=sys.stderr)
        print("If this is `docker run`, the container is uid 10001 and the "
              "directory is yours.", file=sys.stderr)
        print("Run it as yourself:", file=sys.stderr)
        print('  docker run --rm --user "$(id -u):$(id -g)" \\', file=sys.stderr)
        print('    -v "$PWD:/out" <image> init-deploy /out', file=sys.stderr)
        return 1

    for path in written:
        print(f"  wrote {path}")
    for path in kept:
        print(f"  kept  {path} (already there, not overwritten)")

    print()
    print("Next, in that directory:")
    print("  cp .env.example .env")
    print()
    print("Fill in three secrets — the stack will not start without them:")
    print("  CALSYNC_SECRET_RADICALE_PASSWORD          calsync's own account")
    print("  CALSYNC_SECRET_RADICALE_READER_PASSWORD   the one phones use")
    print("  CALSYNC_SECRET_API_TOKEN                  the read API")
    print()
    print("Any three random strings will do:")
    print("  openssl rand -base64 24")
    print()
    print("Then:")
    print("  docker compose up -d")
    print()
    # The calendar address is the one somebody has to type into a phone, so
    # printing it here is cheaper than finding it in a compose file.
    print("It publishes one port, routed by path:")
    print("  http://localhost:8730/       the console")
    print("  http://localhost:8730/cal/   the calendar, for phones")
    print("  http://localhost:8730/v1     the read API")
    print()
    print("`radicale_url` is seeded from CALSYNC_SETTING_RADICALE_URL in the")
    print("compose file, and the poller verifies it before it will start, so a")
    print("stack that cannot reach its calendar fails visibly instead of coming")
    print("up healthy and writing nothing. `docker compose run --rm calsync")
    print("check` asks the same question at any time.")
    return 0


def cmd_set(args) -> int:
    """Set one setting, for scripting what the console does by hand.

    Exists because a stack brought up from a clean clone needs at least one
    value the defaults cannot know — `radicale_url` is `localhost` by default,
    which is right on a laptop and wrong inside every container. Wiring that
    into `scripts/dev-stack.sh` needed a way to say it in one line.

    Unknown keys are refused. `Settings.load` reads a fixed set, so a typo would
    otherwise write a row nothing ever reads and look like it worked.
    """
    conn = db.open_db(args.db)
    if args.key not in db.DEFAULT_SETTINGS:
        print(f"error: no setting called {args.key!r}", file=sys.stderr)
        print("known: " + ", ".join(sorted(db.DEFAULT_SETTINGS)), file=sys.stderr)
        return 2
    set_setting(conn, args.key, args.value)
    print(f"{args.key} = {args.value}")
    return 0


def cmd_check(args) -> int:
    """Ask the configured calendar server whether it is really there.

    The same check the console offers, for a terminal and for a first run — the
    point at which a wrong address is cheap, rather than after a season of polls
    that reported it three lines from the bottom of a log.
    """
    conn = db.open_db(args.db)
    check = targeting.verify(conn, _secrets(args))
    for finding in check.findings:
        print(f"  {'ok ' if finding.ok else 'NO '} {finding.label}: {finding.detail}")
    return 0 if check.ok else 1


def cmd_api(args) -> int:
    """Serve the read API (docs/API.md).

    Separate from the console because the posture is different, not because the
    port is: the console has no login and does not need one, being loopback-only
    with one human operator, whereas this serves programs over a bearer token.
    It refuses to start until that token exists, since a read API for a family's
    schedule that comes up unusable is a thing nobody notices is broken.
    """
    from .api import create_app, serve

    serve(create_app(args.db, secrets=_secrets(args)), host=args.host, port=args.port)
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
        p.add_argument("--target", choices=targeting.KINDS,
                       help="override the configured target_kind for this run")
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
    p_poll.add_argument("--console-url", default="",
                        help="base URL of the console, for links in notifications")
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
    # Which events count as "still to come" now depends on the clock, so this
    # is not just for reproducible tests.
    p_retire.add_argument("--now", help="ISO timestamp to treat as now")
    p_retire.add_argument("--forget", action="store_true",
                          help="also drop the source row, once nothing is left live")
    p_retire.set_defaults(fn=cmd_retire)

    p_digest = sub.add_parser("digest", help="what is on in the next day")
    p_digest.add_argument("--send", action="store_true",
                          help="post it to the configured Matrix room")
    p_digest.add_argument("--hours", type=int, default=24)
    p_digest.add_argument("--now", help="ISO timestamp to treat as now")
    p_digest.add_argument("--secrets", help="path to a secrets JSON file")
    # Named for what it does. A stale source is *always* worth sending, flag or
    # no flag — this only covers the genuinely quiet day.
    p_digest.add_argument("--empty-is-news", action="store_true",
                          help="send even when there is nothing on")
    p_digest.set_defaults(fn=cmd_digest)

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

    p_api = sub.add_parser("api", help="serve the read API for agents")
    p_api.add_argument("--host", default="127.0.0.1",
                       help="bind address (default: loopback; put a VPN in front)")
    p_api.add_argument("--port", type=int, default=8731)
    p_api.add_argument("--secrets", help="path to a secrets JSON file")
    p_api.set_defaults(fn=cmd_api)

    p_init_deploy = sub.add_parser(
        "init-deploy", help="write out compose + server config for a deployment")
    p_init_deploy.add_argument("directory", nargs="?", default=".")
    p_init_deploy.set_defaults(fn=cmd_init_deploy)


    p_set = sub.add_parser("set", help="set one setting")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_set.set_defaults(fn=cmd_set)

    p_check = sub.add_parser("check", help="can the configured calendar be reached?")
    p_check.add_argument("--secrets", help="path to a secrets JSON file")
    p_check.set_defaults(fn=cmd_check)

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
