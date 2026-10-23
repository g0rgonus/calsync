"""The inbound API: a tool writes one team's events, one at a time.

A separate app on a separate port from both the console and the read API, for
the reason the read API is separate from the console — a different posture,
not a different convenience. This is the only listener in calsync meant to be
reachable from outside the household's own network, so it serves nothing but
the routes below: no console, no calendar, no read API, nothing that shares an
origin with a page that has no login.

**Auth is one header, chosen so a proxy's own auth can sit in front of it.**
``X-Calsync-Key`` carries the source's key and ``Authorization`` is never read
— that header belongs to whatever proxy fronts this (Pangolin's Basic Header
Auth, for one), and calsync claiming it too would make the two layers fight
over a single value. For the same reason a 401 from here never says ``Basic``:
a client in "extended compatibility" mode answers a Basic challenge by
resending the proxy's credentials, which can never satisfy this one, and loops.

The key is per source, and the source id is in the path, so a key opens exactly
one team. An unknown source and a wrong key are the same 401, so the API does
not confirm which teams exist to something that cannot write to any of them.

**A write is stored, then synced.** The store row commits first; the sync that
follows is the real `sync_source` against the real target. If the sync cannot
run — the poller holding the write lock, the calendar unreachable — the change
is still stored and the next poll applies it, and the response says which
happened rather than pretending to either.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from wsgiref.simple_server import make_server

from zoneinfo import ZoneInfo

from bottle import Bottle, HTTPResponse, request, response

from .. import db, merging, notify, onboarding, repo, retire, routing, sources, targeting, warmup
from .. import withheld as withheld_mod
from ..api.app import _QuietHandler, _Threaded, _event_json, events_body
from ..secrets import SecretError, SecretStore
from ..settings import Settings
from ..sync import NoTarget, sync_source
from ..targets import TargetError
from . import admin, contract
from . import events as inbound

from .contract import KEY_HEADER, PREFIX, ROUTES  # noqa: F401  (re-exported)
#: A season of events is a few KiB per item; this is generous for one.
MAX_BODY = 64 * 1024
DEFAULT_PORT = 8732
#: Writes per source per minute. A season baseline is a few dozen events, and
#: every write runs a sync against the calendar server, so this is a ceiling on
#: what a leaked key or a looping tool can do to a family calendar, not a quota
#: anybody honest will meet.
WRITES_PER_MINUTE = 60
#: Seconds a connection may sit idle mid-request. The server is a thread per
#: connection, so without this a client that opens sockets and never finishes
#: a request holds threads forever.
SOCKET_TIMEOUT_S = 15
#: How long recording "key last used" may wait for the database before it is
#: skipped. Best effort, so it must never be what a request waits on.
TOUCH_TIMEOUT_MS = 250
#: Compared against when the source does not exist, so an unknown source costs
#: the same work as a wrong key and timing says nothing about which teams exist.
_NO_SUCH_KEY = "0" * 64


class Problem(HTTPResponse):
    """A JSON refusal, the shape `api.app.ApiError` uses, plus field errors."""

    def __init__(self, status: int, code: str, detail: str, *, headers=None, **extra):
        body = {"error": {"code": code, "detail": detail, **extra}}
        super().__init__(
            body=json.dumps(body, indent=2, sort_keys=True) + "\n",
            status=status,
            headers={"Content-Type": "application/json", **(headers or {})},
        )


def _unauthorised(whose: str = "this source's key") -> Problem:
    return Problem(
        401, "bad_credential",
        f"send {whose} in {KEY_HEADER}; Authorization is not read",
        # Not "Basic": see the module docstring.
        headers={"WWW-Authenticate": 'CalsyncKey realm="calsync-inbound"'},
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(body, status: int = 200, headers=None) -> HTTPResponse:
    return HTTPResponse(
        body=json.dumps(body, indent=2, sort_keys=True) + "\n",
        status=status,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def create_app(db_path, *, secrets: SecretStore | None = None, clock=None,
               write_target=None, log=None, writes_per_minute: int = WRITES_PER_MINUTE,
               monotonic=time.monotonic, notify_sender=notify.send) -> Bottle:
    """Build the inbound API.

    ``write_target`` is the seam the console has: a test injects a target, a
    deployment gets `targeting.build_target`. Unlike the read API this starts
    with no credential configured — keys belong to sources, and a deployment
    with no inbound source yet simply refuses every write.
    """
    app = Bottle()
    secrets = secrets or SecretStore()
    clock = clock or _now
    # Flushed: in a container stdout is a pipe, and a buffered audit line
    # reaches the log late or, if the process dies, never.
    log = log or (lambda line: print(line, flush=True))
    db_path = str(db_path)
    locks: dict[str, threading.RLock] = {}
    locks_guard = threading.Lock()
    recent_writes: dict[str, deque] = {}

    with closing(db.open_db(db_path)):
        pass

    def connect():
        return closing(db.connect(db_path))

    def lock_for(source_id: str) -> threading.RLock:
        """One writer per team at a time, from the first read to the end of the
        sync. Re-entrant, because the sync a write ends with takes it too."""
        with locks_guard:
            return locks.setdefault(source_id, threading.RLock())

    @contextmanager
    def transaction():
        """Read, check and save under SQLite's write lock.

        The team lock above serialises this process. This covers anything else
        that could write the same rows — a second replica of this service —
        and it is taken before the first read, so two writers cannot both read
        the same event, both pass `If-Match`, and have the second silently
        replace the first. Closing without a commit rolls back, so a refusal
        raised part-way leaves nothing behind.
        """
        with connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                # The poller held the database past the busy timeout. Nothing
                # was read or written; the same request will work in a moment.
                raise Problem(503, "busy", "calsync is writing; retry shortly",
                              headers={"Retry-After": "5"}) from None
            yield conn

    # --- errors as JSON, always ---------------------------------------------

    def _json_error(err):
        err.content_type = "application/json"
        return json.dumps(
            {"error": {"code": f"http_{err.status_code}",
                       "detail": str(err.body or err.status_line)}},
            indent=2, sort_keys=True,
        ) + "\n"

    app.default_error_handler = _json_error

    @app.hook("after_request")
    def _hygiene():
        # A child's whereabouts, so never cached by anything in between; and a
        # JSON body is never to be sniffed into something a browser would run.
        response.set_header("Cache-Control", "no-store")
        response.set_header("X-Content-Type-Options", "nosniff")

    # --- auth ----------------------------------------------------------------

    def client() -> str:
        """Who is calling, for the log — never a control.

        The connecting address is always given, because it is the only part
        nobody can make up. A forwarded-for is shown beside it as what the
        caller *claims*: behind Pangolin that is the real client, and on the
        LAN anything could have written it.
        """
        peer = request.environ.get("REMOTE_ADDR") or "?"
        forwarded = (request.get_header("X-Forwarded-For") or "").split(",")[0].strip()
        return f"{forwarded} via {peer}" if forwarded else peer

    def refuse(conn, presented: str, whose: str = "this source's key", *,
               opens=lambda key: True) -> Problem:
        """A 401, saying "revoked" only for a revoked key that once opened this route."""
        old = repo.revoked_key(conn, inbound.hash_key(presented)) if presented else None
        if old and opens(old):
            return Problem(401, "revoked", f"this key was revoked at {old.revoked_at}; "
                                           "ask for a new one",
                           headers={"WWW-Authenticate": 'CalsyncKey realm="calsync-inbound"'})
        return _unauthorised(whose)

    def authorise(conn, source_id: str) -> repo.Source:
        presented = (request.get_header(KEY_HEADER) or "").strip()
        source = repo.get_source(conn, source_id)
        stored = repo.inbound_key_hash(conn, source_id) if source else None
        usable = source is not None and source.kind in sources.PUSHED and stored
        # Always one comparison, against a dummy when there is nothing real.
        matched = inbound.key_matches(presented, stored if usable else _NO_SUCH_KEY)
        if not (usable and matched):
            # Logged so a key being guessed at — or a tool sent the old key
            # after a rotation — shows up somewhere. The presented value is not.
            log(f"inbound {source_id} refused: "
                f"{'no key' if not presented else 'wrong key'} from {client()}")
            raise refuse(conn, presented,
                         opens=lambda k: k.scope == "team" and k.source_id == source_id)
        touch(conn, lambda: repo.inbound_key_used_at(conn, source_id),
              lambda at: repo.touch_inbound_key(conn, source_id, at))
        return source

    def authorise_reader(conn) -> None:
        """The household read key: every team's calendar, and nothing else.

        An admin key reads it too, to find the events it changes.

        Checked the same way as a team key — one comparison, against a dummy
        when no key is set — and refused the same way, so the response says
        nothing about whether a read key exists. A team key is not one: each
        opens a single team, and the whole family's schedule is a wider grant.
        """
        presented = (request.get_header(KEY_HEADER) or "").strip()
        stored = repo.read_key_hash(conn)
        matched = inbound.key_matches(presented, stored or _NO_SUCH_KEY)
        admin_ = None if matched or not presented else repo.admin_key(
            conn, inbound.hash_key(presented))
        if admin_:
            touch(conn, lambda: repo.key_used_at(conn, admin_.id),
                  lambda at: repo.touch_key(conn, admin_.id, at))
            return
        if not (stored and matched):
            log(f"inbound calendar refused: "
                f"{'no key' if not presented else 'wrong key'} from {client()}")
            raise refuse(conn, presented, "the household read key or an admin key",
                         opens=lambda k: k.scope in ("household_read", "admin"))
        touch(conn, lambda: repo.read_key_used_at(conn),
              lambda at: repo.touch_read_key(conn, at))

    def touch(conn, last_used, record) -> None:
        """Record that the key was used, at most once a minute.

        The console shows the last *write*; a key used only to read the
        schedule would otherwise leave no trace, and reading is what a leaked
        key is for. Best effort: a busy database costs the timestamp, never
        the request.
        """
        now = clock()
        last = last_used()
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=1):
            return
        # A short wait of its own: with the connection's normal timeout, a
        # poller holding the database would stall an ordinary GET for seconds
        # over a timestamp that is allowed to be skipped.
        conn.execute(f"PRAGMA busy_timeout = {TOUCH_TIMEOUT_MS}")
        try:
            record(now.isoformat())
            conn.commit()
        except sqlite3.OperationalError:
            conn.rollback()
        finally:
            conn.execute(f"PRAGMA busy_timeout = {db.BUSY_TIMEOUT_MS}")

    def throttle(source_id: str) -> None:
        now = monotonic()
        with locks_guard:
            window = recent_writes.setdefault(source_id, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= writes_per_minute:
                retry = int(60 - (now - window[0])) + 1
                raise Problem(429, "too_many_writes",
                              f"at most {writes_per_minute} writes a minute per team",
                              headers={"Retry-After": str(retry)})
            window.append(now)

    def body() -> object:
        length = request.content_length
        if length > MAX_BODY:
            raise Problem(413, "too_large", f"at most {MAX_BODY} bytes per request")
        raw = request.body.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise Problem(413, "too_large", f"at most {MAX_BODY} bytes per request")
        try:
            return json.loads(raw or b"null")
        except ValueError as exc:
            raise Problem(400, "bad_json", f"the body is not JSON: {exc}") from exc

    def check_match(item: repo.InboundItem | None) -> None:
        wanted = request.get_header("If-Match")
        if wanted is None:
            return
        # A weak tag is compared as if strong. Ours are hashes of content, and
        # a compressing proxy — Traefik, under Pangolin — rewrites them to W/
        # on the way out; refusing the echo would make every conditional
        # write through it a 412.
        tags = {t.strip().removeprefix("W/").strip('"') for t in wanted.split(",")}
        if "*" in tags and item is not None:
            return
        if item is None or item.etag not in tags:
            raise Problem(412, "etag_mismatch",
                          "the event has changed since that ETag; GET it and retry",
                          current=item.etag if item else None)

    def writable(source: repo.Source) -> None:
        if not source.enabled:
            raise Problem(409, "source_disabled",
                          "polling is stopped for this team (paused or retired), so "
                          "nothing is written to its calendar; resume it in the console")

    def activity_tz(conn, source: repo.Source) -> str:
        return repo.get_activity(conn, source.activity_id).tz

    def invalid(exc: inbound.Invalid) -> Problem:
        return Problem(422, "invalid", "the event was not stored", errors=exc.errors)

    def prune(state: dict, conn) -> dict:
        """Forget retired occurrences older than anything the calendar keeps."""
        back = Settings.load(conn).sync_window_back_days
        cutoff = (clock() - timedelta(days=back)).isoformat()
        state["retired"] = [r for r in state.get("retired", []) if r["end"] >= cutoff]
        return state

    def sync(source: repo.Source) -> dict:
        """Run the real sync for this source now, and say what it did."""
        with lock_for(source.id), connect() as conn:
            try:
                target = write_target or targeting.build_target(conn, secrets=secrets)
                fresh = repo.get_source(conn, source.id)
                report = sync_source(conn, fresh, target, now=clock(), secrets=secrets)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                return {"status": "deferred",
                        "detail": "stored; the poller was writing, so the next poll "
                                  "applies it"}
            except (SecretError, TargetError) as exc:
                return {"status": "deferred",
                        "detail": f"stored; the calendar could not be reached ({exc}), "
                                  "so the next poll applies it"}
        out = {"status": report.status, "summary": report.line()}
        if report.staged_to:
            out["staged_to"] = report.staged_to
        if report.held:
            out["held"] = report.held
        if report.errors:
            out["errors"] = report.errors
        if report.merge_errors:
            out["merge_errors"] = report.merge_errors
        return out

    def parts_index(conn, source: repo.Source) -> dict[str, list[str]]:
        return inbound.live_parts(
            (i.external_id, i.state, i.cancelled)
            for i in repo.inbound_items(conn, source.id))

    def render_item(conn, source: repo.Source, item: repo.InboundItem,
                    window=None, parts=None) -> dict:
        tz = activity_tz(conn, source)
        if parts is None:
            parts = parts_index(conn, source)
        # A meet its sessions have replaced is not cancelled — nobody asked for
        # that — but it is not on any calendar either. Both are said, so a
        # client listing before it writes is not told a hidden meet is live.
        superseded_by = parts.get(item.external_id, [])
        occurrences = []
        for i in inbound.instances(source.id, item.external_id, item.state,
                                   default_tz=tz):
            day = i.start.astimezone(ZoneInfo(i.tz)).date()
            if window and not (window[0] <= day <= window[1]):
                continue
            occurrences.append(
                {"date": day.isoformat(), "uid": i.uid, "start": i.start.isoformat(),
                 "end": i.end.isoformat(), "all_day": i.all_day})
        return {
            "source_id": source.id,
            "external_id": item.external_id,
            "etag": item.etag,
            "cancelled": item.cancelled,
            "superseded_by": superseded_by,
            "on_calendar": not item.cancelled and not superseded_by,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "event": item.state["event"],
            "overrides": item.state.get("overrides") or {},
            "occurrences": occurrences,
        }

    def date_window():
        """``?from=&to=`` as local dates, inclusive, or None for everything."""
        raw = {k: request.query.get(k) for k in ("from", "to")}
        if not any(raw.values()):
            return None
        bounds = []
        for name in ("from", "to"):
            try:
                bounds.append(date.fromisoformat(raw[name]) if raw[name] else None)
            except ValueError:
                raise Problem(422, "invalid", "the window is not usable",
                              errors=[{"field": name, "code": "bad_date",
                                       "detail": "a date, YYYY-MM-DD"}]) from None
        return (bounds[0] or date.min, bounds[1] or date.max)

    def external(external_id: str) -> str:
        if not inbound.EXTERNAL_ID.match(external_id):
            raise Problem(422, "invalid", "the event id is not usable",
                          errors=[{"field": "external_id", "code": "bad_id",
                                   "detail": "1-128 of A-Z a-z 0-9 . _ -, starting "
                                             "with a letter or digit"}])
        return external_id

    def audit(source_id: str, action: str, external_id: str, outcome: str) -> None:
        # Who, what and how it went. Never the key and never the body: the body
        # is a child's whereabouts, and this goes to a container log.
        log(f"inbound {source_id} {action} {external_id}: {outcome}")

    # --- description -----------------------------------------------------------

    def server() -> str:
        """The address the caller used, for the spec and the examples.

        Behind a proxy that is the proxy's, which is the one a tool must call.
        Only ever echoed back escaped, so a forged Host header buys nothing.
        """
        proto = (request.get_header("X-Forwarded-Proto") or request.urlparts.scheme)
        host = (request.get_header("X-Forwarded-Host") or request.get_header("Host")
                or "localhost")
        return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"

    @app.get(PREFIX)
    def describe():
        return _dump(contract.describe())

    @app.get(PREFIX + "/docs")
    def docs():
        return HTTPResponse(
            body=contract.page(server()),
            headers={
                "Content-Type": "text/html; charset=utf-8",
                # Inline styles and nothing else: no script, no outside fetch,
                # and no framing, on a page anyone past the proxy can open.
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                           "frame-ancestors 'none'; base-uri 'none'",
            },
        )

    @app.get(PREFIX + "/openapi.json")
    def openapi():
        return _dump(contract.openapi(server()))

    # --- the household calendar --------------------------------------------------

    @app.get(PREFIX + "/calendar")
    def calendar():
        """Every team's events, read-only, exactly as `GET /v1/events` serves them.

        The same function builds both, so a tool reading here and the Mac
        mirror reading the read API cannot be told two different things. Its
        limits come with it: a bounded window, and only what calsync wrote — a
        hand-made appointment is not in calsync and so is not here either.
        """
        with connect() as conn:
            authorise_reader(conn)
            body = events_body(conn, request.query, now=clock())
        audit("household", "GET calendar", f"{body['count']} events", "200")
        return _dump(body)

    # --- the source --------------------------------------------------------------

    @app.get(PREFIX + "/sources/<source_id>")
    def whoami(source_id):
        with connect() as conn:
            source = authorise(conn, source_id)
            activity = repo.get_activity(conn, source.activity_id)
            child = repo.get_child(conn, activity.child_id)
            return _dump({
                "source_id": source.id,
                "team": activity.name,
                "sport": activity.sport,
                "child": child.name,
                "tz": activity.tz,
                "enabled": source.enabled,
                "staged_to": source.staging_collection,
                "last_push_at": repo.last_push(conn, source.id),
            })

    @app.get(PREFIX + "/sources/<source_id>/events")
    def list_items(source_id):
        with connect() as conn:
            source = authorise(conn, source_id)
            window = date_window()
            parts = parts_index(conn, source)
            rendered = [render_item(conn, source, i, window, parts)
                        for i in repo.inbound_items(conn, source.id)]
            if window:
                # Only events with a date in the window, carrying only those
                # dates: "what is on the 27th" should not need a season read.
                rendered = [r for r in rendered if r["occurrences"]]
            return _dump({"source_id": source.id, "count": len(rendered),
                          "events": rendered})

    @app.get(PREFIX + "/sources/<source_id>/events/<external_id>")
    def get_item(source_id, external_id):
        with connect() as conn:
            source = authorise(conn, source_id)
            item = repo.inbound_item(conn, source.id, external(external_id))
            if item is None:
                raise Problem(404, "no_such_event", "nothing has been sent with that id")
            return _dump(render_item(conn, source, item), headers={"ETag": f'"{item.etag}"'})

    # --- writes --------------------------------------------------------------------

    def store(conn, source, external_id, state, *, cancelled: bool) -> repo.InboundItem:
        state = prune(state, conn)
        tag = inbound.etag(state, cancelled)
        repo.save_inbound_item(conn, source.id, external_id, state=state,
                               cancelled=cancelled, etag=tag, now=clock().isoformat())
        conn.commit()
        return repo.inbound_item(conn, source.id, external_id)

    def admit(source_id: str, external_id: str) -> repo.Source:
        """Everything that needs no body and no lock, in that order.

        The key is checked before a byte of the body is read, so an
        unauthenticated caller costs a header lookup and a hash, not a JSON
        parse of up to `MAX_BODY`.
        """
        with connect() as conn:
            source = authorise(conn, source_id)
        external(external_id)
        writable(source)
        throttle(source.id)
        return source

    @app.put(PREFIX + "/sources/<source_id>/events/<external_id>")
    def put_item(source_id, external_id):
        source = admit(source_id, external_id)
        payload = body()
        with lock_for(source.id):
            with transaction() as conn:
                old = repo.inbound_item(conn, source.id, external_id)
                check_match(old)
                if old is not None and request.get_header("If-None-Match", "").strip() == "*":
                    # The sender said "this is new". Replacing an existing event
                    # instead is exactly the mistake it was guarding against.
                    raise Problem(412, "already_exists",
                                  "an event with this id was already sent; GET it, then "
                                  "PUT without If-None-Match to change it",
                                  current=old.etag)
                tz = activity_tz(conn, source)
                try:
                    event = inbound.validate_event(payload, default_tz=tz)
                except inbound.Invalid as exc:
                    audit(source.id, "PUT", external_id, "422")
                    raise invalid(exc) from None
                items = repo.inbound_items(conn, source.id)
                problems = inbound.parent_problems(
                    external_id, event, {i.external_id: i.state for i in items})
                if problems:
                    audit(source.id, "PUT", external_id, "422 part_of")
                    raise invalid(inbound.Invalid(problems))
                # Allowed, because it can be right — a meet deleted under the
                # old delete-then-send workaround is exactly this — but worth
                # saying, since it can also mean the sender missed a cancellation.
                warnings = []
                parent = event.get("part_of")
                if parent and any(i.external_id == parent and i.cancelled for i in items):
                    warnings.append(f"part_of names {parent!r}, which is cancelled; "
                                    "this event is written regardless")
                state, orphaned = inbound.put_event(
                    source.id, external_id, old.state if old else None, event,
                    default_tz=tz)
                if old is None and request.query.get("allow_overlap") not in ("1", "true"):
                    # A meet its sessions replaced is on no calendar, so it
                    # cannot be what a new event duplicates. Its sessions are
                    # on the calendar, and they still count.
                    hidden = inbound.superseded(
                        (i.external_id, i.state, i.cancelled) for i in items)
                    others = [(i.external_id, i.state) for i in items
                              if not i.cancelled and i.external_id not in hidden]
                    clash = inbound.overlaps(source.id, external_id, state, others,
                                             default_tz=tz)
                    if clash:
                        audit(source.id, "PUT", external_id, "409 possible duplicate")
                        raise Problem(
                            409, "possible_duplicate",
                            "a live event of the same type overlaps this one; list the "
                            "events and update that one by its id, or resend with "
                            "?allow_overlap=true if both are real",
                            duplicates=clash[:20],
                        )
                # An identical PUT is a no-op all the way down: same ETag, no
                # write, no sync. Re-sending a season baseline must cost nothing.
                if old is not None and not old.cancelled \
                        and inbound.etag(state, False) == old.etag:
                    audit(source.id, "PUT", external_id, "200 unchanged")
                    out = render_item(conn, source, old)
                    out.update(changed=False, orphaned_overrides=[], warnings=warnings)
                    return _dump(out, headers={"ETag": f'"{old.etag}"'})
                item = store(conn, source, external_id, state, cancelled=False)
            result = sync(source)
        with connect() as conn:
            out = render_item(conn, source, item)
        out.update(changed=True, orphaned_overrides=orphaned, sync=result,
                   warnings=warnings)
        status = 200 if old is not None else 201
        audit(source.id, "PUT", external_id, f"{status} sync {result['status']}")
        return _dump(out, status=status, headers={"ETag": f'"{item.etag}"'})

    @app.delete(PREFIX + "/sources/<source_id>/events/<external_id>")
    def delete_item(source_id, external_id):
        source = admit(source_id, external_id)
        with lock_for(source.id):
            with transaction() as conn:
                old = repo.inbound_item(conn, source.id, external_id)
                if old is None:
                    raise Problem(404, "no_such_event", "nothing has been sent with that id")
                check_match(old)
                if old.cancelled:
                    audit(source.id, "DELETE", external_id, "204 already cancelled")
                    return HTTPResponse(status=204)
                store(conn, source, external_id, dict(old.state), cancelled=True)
            result = sync(source)
        audit(source.id, "DELETE", external_id, f"204 sync {result['status']}")
        return HTTPResponse(status=204, headers={"X-Calsync-Sync": result["status"]})

    def occurrence(source_id, external_id, date, *, cancel: bool):
        source = admit(source_id, external_id)
        payload = None if cancel else body()
        with lock_for(source.id):
            with transaction() as conn:
                old = repo.inbound_item(conn, source.id, external_id)
                if old is None:
                    raise Problem(404, "no_such_event", "nothing has been sent with that id")
                check_match(old)
                if old.cancelled:
                    raise Problem(409, "event_cancelled",
                                  "the whole event is cancelled; PUT it again first")
                tz = activity_tz(conn, source)
                try:
                    if cancel:
                        state = inbound.cancel_occurrence(
                            source.id, external_id, old.state, date, default_tz=tz)
                    else:
                        if not old.state["event"].get("rrule"):
                            raise inbound.NotAnOccurrence(
                                "this event does not repeat; PUT the event itself instead")
                        change = inbound.validate_override(
                            payload, event=old.state["event"], key=date, default_tz=tz)
                        state = inbound.put_override(source.id, external_id, old.state,
                                                     date, change, default_tz=tz)
                except inbound.NotAnOccurrence as exc:
                    raise Problem(422, "not_an_occurrence", str(exc)) from None
                except inbound.Invalid as exc:
                    raise invalid(exc) from None
                if inbound.etag(state, False) == old.etag:
                    audit(source.id, "occurrence", f"{external_id}/{date}", "unchanged")
                    if cancel:
                        return HTTPResponse(status=204)
                    out = render_item(conn, source, old)
                    out.update(changed=False)
                    return _dump(out, headers={"ETag": f'"{old.etag}"'})
                item = store(conn, source, external_id, state, cancelled=False)
            result = sync(source)
        audit(source.id, "DELETE occurrence" if cancel else "PUT occurrence",
              f"{external_id}/{date}", f"sync {result['status']}")
        if cancel:
            return HTTPResponse(status=204, headers={"X-Calsync-Sync": result["status"]})
        with connect() as conn:
            out = render_item(conn, source, item)
        out.update(changed=True, sync=result)
        return _dump(out, headers={"ETag": f'"{item.etag}"'})

    @app.put(PREFIX + "/sources/<source_id>/events/<external_id>/occurrences/<date>")
    def put_occurrence(source_id, external_id, date):
        return occurrence(source_id, external_id, date, cancel=False)

    @app.delete(PREFIX + "/sources/<source_id>/events/<external_id>/occurrences/<date>")
    def delete_occurrence(source_id, external_id, date):
        return occurrence(source_id, external_id, date, cancel=True)

    # --- admin ----------------------------------------------------------------------

    ADMIN = PREFIX + "/admin"

    def authorise_admin(conn) -> repo.ApiKey:
        presented = (request.get_header(KEY_HEADER) or "").strip()
        key = repo.admin_key(conn, inbound.hash_key(presented)) if presented else None
        if key is None:
            log(f"inbound admin refused: {'no key' if not presented else 'wrong key'} "
                f"from {client()}")
            raise refuse(conn, presented, "an admin key", opens=lambda k: k.scope == "admin")
        touch(conn, lambda: repo.key_used_at(conn, key.id),
              lambda at: repo.touch_key(conn, key.id, at))
        return key

    def admin_source(conn, source_id: str) -> repo.Source:
        source = repo.get_source(conn, source_id)
        if source is None:
            raise Problem(404, "no_such_team", "no team has that id")
        return source

    def team_body(conn, source: repo.Source) -> dict:
        activity = repo.get_activity(conn, source.activity_id)
        key = repo.team_key(conn, source.id)
        return {
            "source_id": source.id,
            "activity_id": activity.id,
            "name": activity.name,
            "children": [c.id for c in repo.team_children(conn, activity)],
            "sport": activity.sport,
            "tz": activity.tz,
            "kind": source.kind,
            "enabled": source.enabled,
            "staged_to": source.staging_collection,
            "key": None if key is None else {
                "id": key.id, "created_at": key.created_at,
                "last_used_at": key.last_used_at},
        }

    @app.get(ADMIN + "/teams")
    def list_teams():
        with connect() as conn:
            authorise_admin(conn)
            teams = [team_body(conn, s) for s in repo.list_sources(conn, enabled_only=False)]
        return _dump({"count": len(teams), "teams": teams})

    @app.post(ADMIN + "/teams")
    def create_team():
        """Create a team from typed fields. It starts staged, like the console's."""
        idem = (request.get_header("Idempotency-Key") or "").strip()
        with connect() as conn:
            caller = authorise_admin(conn)
        if not admin.IDEMPOTENCY_KEY.match(idem):
            raise Problem(400, "idempotency_key_required",
                          "send Idempotency-Key: a unique value per intended create, "
                          "1-128 of A-Z a-z 0-9 . _ : -")
        throttle(f"admin:{caller.id}")
        payload = body()
        digest = admin.request_hash(payload)
        # One create at a time: onboarding commits as it goes, so the duplicate
        # and replay checks need this to see each other's results.
        with lock_for("admin"), connect() as conn:
            stored = repo.idempotent_result(conn, caller.id, idem, now=clock())
            if stored:
                if stored["request_sha256"] != digest:
                    raise Problem(422, "idempotency_key_reused",
                                  "this Idempotency-Key was used with a different body")
                replay = json.loads(stored["body"])
                if repo.get_source(conn, replay["source_id"]) is None:
                    raise Problem(410, "team_discarded",
                                  "the team this request created has been discarded")
                replay["key_already_issued"] = True
                return _dump(replay, stored["status"], headers={"Idempotent-Replayed": "true"})
            try:
                fields = admin.validate_team(payload, conn)
            except inbound.Invalid as exc:
                raise Problem(422, "invalid", "the team was not created",
                              errors=exc.errors) from None
            child_id = fields["children"][0]
            if len(fields["children"]) > 1 and \
                    "{child}" in Settings.load(conn).collection_template:
                raise Problem(422, "per_kid_calendars", "calendars are split by kid, so a "
                              "shared team is not available here")
            existing = next(filter(None, (repo.team_named(conn, c, fields["name"])
                                          for c in fields["children"])), None)
            if existing:
                raise Problem(409, "already_exists",
                              "this kid already has a team with that name",
                              team=team_body(conn, repo.get_source(conn, existing)))
            previous = onboarding.clone_forward(conn, child_id, fields["sport"])
            draft = onboarding.Draft(
                url="", child_id=child_id, sport=fields["sport"],
                team_name=fields["name"].strip(), kind="inbound",
                tz=(fields.get("tz") or (previous.tz if previous else None)
                    or Settings.load(conn).default_tz),
                season_end=fields.get("season_end"),
                emoji=previous.emoji if previous else None,
                league=previous.league if previous else None,
                age_group=previous.age_group if previous else None,
                alarm_game_min=fields.get(
                    "alarm_game_min", previous.alarm_game_min if previous else 90),
                alarm_practice_min=fields.get(
                    "alarm_practice_min", previous.alarm_practice_min if previous else 30),
            )
            created = onboarding.create(conn, draft, secrets=secrets, actor=caller.label)
            for other in fields["children"][1:]:
                repo.add_team_child(conn, created.activity_id, other)
            team = team_body(conn, repo.get_source(conn, created.source_id))
            repo.log_admin(conn, caller.label, "team_created", f"team:{created.source_id}")
            repo.save_idempotent(conn, caller.id, idem, request_sha256=digest,
                                 status=201, body=team, now=clock())
            conn.commit()
        audit(created.source_id, "POST admin/teams", idem, "201")
        return _dump({**team, "team_key": created.key}, 201)

    def set_team_enabled(source_id: str, enabled: bool):
        with connect() as conn:
            caller = authorise_admin(conn)
            source = admin_source(conn, source_id)
            repo.log_admin(conn, caller.label, "team_resumed" if enabled else "team_paused",
                           f"team:{source_id}")
            repo.set_enabled(conn, source_id, enabled)
            if not source.staging_collection and source.enabled != enabled:
                alert(conn, f"{caller.label} {'resumed' if enabled else 'paused'} "
                            f"{team_name(conn, source)}.")
            return _dump(team_body(conn, repo.get_source(conn, source_id)))

    @app.post(ADMIN + "/teams/<source_id>/pause")
    def pause_team(source_id):
        return set_team_enabled(source_id, False)

    @app.post(ADMIN + "/teams/<source_id>/resume")
    def resume_team(source_id):
        return set_team_enabled(source_id, True)

    @app.post(ADMIN + "/teams/<source_id>/key")
    def new_team_key(source_id):
        """A new key for a pushed team. The old one stops working at once."""
        with connect() as conn:
            caller = authorise_admin(conn)
            source = admin_source(conn, source_id)
            if source.kind not in sources.PUSHED:
                raise Problem(409, "not_pushed", "this team is read from a feed; it has no key")
            key = inbound.new_key()
            repo.set_inbound_key_hash(conn, source_id, inbound.hash_key(key),
                                      actor=caller.label)
            return _dump({"source_id": source_id, "team_key": key,
                          "key_id": repo.team_key(conn, source_id).id}, 201)

    @app.delete(ADMIN + "/teams/<source_id>/key")
    def revoke_team_key(source_id):
        with connect() as conn:
            caller = authorise_admin(conn)
            admin_source(conn, source_id)
            repo.revoke_team_key(conn, source_id, actor=caller.label)
        return HTTPResponse(status=204)

    # --- admin: staging ----------------------------------------------------------------

    def staged_events(conn, activity, settings) -> list:
        now = clock()
        return repo.stored_events(
            conn, start=(now - timedelta(days=settings.sync_window_back_days)).isoformat(),
            end=(now + timedelta(days=settings.sync_window_forward_days)).isoformat(),
            activity_id=activity.id)

    def clashes(conn, mine: list, settings) -> list[dict]:
        """Timed events of other teams that overlap this team's timed events."""
        if not mine:
            return []
        live = [e for e in mine if not e.cancelled and not e.event.all_day]
        start = min(e.event.starts_at for e in mine).isoformat()
        end = max(e.event.ends_at for e in mine).isoformat()
        others = [o for o in repo.stored_events(conn, start=start, end=end)
                  if o.activity_id != mine[0].activity_id and not o.cancelled
                  and not o.event.all_day]
        out = []
        for e in live:
            for o in others:
                if o.event.starts_at < e.event.ends_at and e.event.starts_at < o.event.ends_at:
                    other = _event_json(conn, o, settings)
                    out.append({"uid": e.event.uid, "starts_at": e.event.starts_at.isoformat(),
                                "with": {k: other[k] for k in (
                                    "uid", "child", "activity", "summary_rendered",
                                    "starts_at", "ends_at")}})
        return out

    def preview(conn, source):
        return sync_source(conn, source, NoTarget(), now=clock(), secrets=secrets,
                           dry_run=True)

    def not_ready(report) -> str:
        if report.pushed and report.events_seen == 0:
            return "nothing has been sent yet"
        if not report.pushed and report.fixtures_seen == 0:
            return "no games have appeared yet"
        return "the parse still has gaps"

    @app.get(ADMIN + "/teams/<source_id>/review")
    def review_team(source_id):
        """What making this team live would put on the calendars, checked now."""
        from ..web import gate   # here, not at the top: web imports this module
        with connect() as conn:
            authorise_admin(conn)
            source = admin_source(conn, source_id)
            report = preview(conn, source)
            activity = repo.get_activity(conn, source.activity_id)
            child = repo.get_child(conn, activity.child_id)
            settings = Settings.load(conn)
            mine = staged_events(conn, activity, settings)
            events = []
            for item in mine:
                body_ = _event_json(conn, item, settings)
                body_["lands_in"] = routing.collection_for(item.event, activity, child, settings)
                events.append(body_)
            return _dump({
                "team": team_body(conn, source),
                "promotable": report.promotable,
                "not_ready": None if report.promotable else not_ready(report),
                "checks": [{"key": c.key, "label": c.label, "state": c.state,
                            "headline": c.headline, "detail": c.detail,
                            "items": list(c.items)}
                           for c in gate.conditions(report, activity)],
                "events": events,
                "clashes": clashes(conn, mine, settings),
            })

    @app.post(ADMIN + "/teams/<source_id>/live")
    def make_live(source_id):
        """Promote a staged team behind the console's gate, and move its events.

        No override here: only a person in the console can force one through.
        """
        with lock_for(source_id), connect() as conn:
            caller = authorise_admin(conn)
            source = admin_source(conn, source_id)
            if not source.staging_collection:
                return _dump(team_body(conn, source))
            report = preview(conn, source)
            if not report.promotable:
                raise Problem(409, "not_ready", f"not made live: {not_ready(report)}",
                              hint="GET the review for the checks; a person can "
                                   "answer them in the console")
            repo.log_admin(conn, caller.label, "team_made_live", f"team:{source_id}")
            repo.set_staging(conn, source_id, None)
            alert(conn, f"{caller.label} made {team_name(conn, source)} live.")
            team = team_body(conn, repo.get_source(conn, source_id))
        return _dump({**team, "sync": sync(source)})

    @app.delete(ADMIN + "/teams/<source_id>")
    def discard_team(source_id):
        """Throw away a staged team: its events leave the staging calendar, then its rows go.

        A live team is refused; taking one off the family's calendars stays in
        the console.
        """
        with lock_for(source_id), connect() as conn:
            caller = authorise_admin(conn)
            source = admin_source(conn, source_id)
            if not source.staging_collection:
                raise Problem(409, "not_staged", "only a staged team can be discarded; "
                              "retire a live team in the console")
            try:
                target = write_target or targeting.build_target(conn, secrets=secrets)
                # From the start of time, so past events go too: a staged team's
                # events were never on the family's calendars.
                report = retire.retire_source(conn, source, target,
                                              now=datetime.min.replace(tzinfo=timezone.utc))
            except (SecretError, TargetError) as exc:
                raise Problem(503, "calendar_unreachable",
                              f"the team was not discarded: {exc}",
                              headers={"Retry-After": "60"}) from None
            if not report.ok:
                raise Problem(503, "calendar_unreachable",
                              f"{report.cancelled} staged events removed, "
                              f"{len(report.errors)} could not be; the team was not "
                              "discarded, retry", headers={"Retry-After": "60"})
            repo.log_admin(conn, caller.label, "team_discarded", f"team:{source_id}")
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            conn.execute("DELETE FROM activities WHERE id = ? AND NOT EXISTS "
                         "(SELECT 1 FROM sources WHERE activity_id = ?)",
                         (source.activity_id, source.activity_id))
            conn.commit()
        audit(source_id, "DELETE admin/teams", source_id, "204")
        return HTTPResponse(status=204)

    # --- admin: event status -------------------------------------------------------

    @app.post(ADMIN + "/events/status")
    def set_event_status():
        """Mark one event not attending or cancelled, or put it back.

        The console's withhold and restore, reached by a tool: the same flag,
        and putting back is an ordinary sync. Only hiding refuses a started
        event, so a mistake can always be undone. Hiding sends a push.
        """
        with connect() as conn:
            caller = authorise_admin(conn)
        throttle(f"admin:{caller.id}")
        try:
            change = admin.validate_status(body())
        except inbound.Invalid as exc:
            raise Problem(422, "invalid", "nothing was changed", errors=exc.errors) from None
        uid, status = change["uid"], change["status"]
        if warmup.is_synthetic(uid):
            raise Problem(422, "warmup", "a warm-up follows its game; change the game",
                          game=warmup.parent_of(uid))
        with connect() as conn:
            found = repo.event_state(conn, uid)
        if found is None:
            raise Problem(404, "no_such_event", "calsync has not written an event with that uid")
        with lock_for(found.source_id), connect() as conn:
            state = repo.event_state(conn, uid)
            if state is None:
                raise Problem(404, "no_such_event",
                              "calsync has not written an event with that uid")
            child = change.get("child")
            if child is not None:
                return set_kid_absent(conn, caller, state, child, status is not None,
                                      change.get("note"))
            if status is None and not state.withheld:
                return _dump({"uid": uid, "status": None, "on_calendar": not state.cancelled,
                              "detail": "it was not marked"})
            if status is not None and not retire.upcoming(state.shown_starts_at, clock()):
                raise Problem(409, "already_started",
                              "that event has started; taking it off would remove the "
                              "record of something that happened")
            repo.set_withheld(conn, uid, status)
            repo.log_admin(conn, caller.label,
                           "event_restored" if status is None else "event_withheld",
                           f"event:{uid}", {"status": status, "note": change.get("note")})
            conn.commit()
            source = repo.get_source(conn, state.source_id)
            if status is None and source.kind == "merged":
                # Written by the merge step, never synced: it has no feed.
                result = {"sync": publish_merges(conn)}
            elif status is None:
                result = {"sync": (sync(source) if source.enabled else
                          {"status": "deferred", "detail": "the team is paused; the "
                           "event comes back when it resumes"})}
            else:
                unreachable = None
                merge_errors: list[str] = []
                try:
                    target = write_target or targeting.build_target(conn, secrets=secrets)
                    withheld_mod.enforce(conn, state.source_id, target)
                    conn.commit()
                    merge_errors = merging.publish_safely(
                        conn, target, now=clock(), secrets=secrets).errors
                except (SecretError, TargetError) as exc:
                    unreachable = exc
            event_alert(conn, caller, state, status, change.get("note"))
            # This event's own record, not the team's report: another event
            # failing to come off says nothing about this one.
            off = repo.event_state(conn, uid).cancelled
            if status is not None:
                result = {"detail": "off the calendar" if off else
                          f"saved; the calendar could not be reached ({unreachable}), so "
                          "the next sync removes it" if unreachable else
                          "saved; removing it failed, so a later sync retries",
                          **({"merge_errors": merge_errors} if merge_errors else {})}
        audit(state.source_id, "POST admin/events/status", uid, str(status))
        return _dump({"uid": uid, "status": status, "on_calendar": not off, **result})

    def publish_merges(conn) -> dict:
        """Run the merge step now (it reads the database, no feed), and say how it went."""
        try:
            target = write_target or targeting.build_target(conn, secrets=secrets)
            report = merging.publish_safely(conn, target, now=clock(), secrets=secrets)
        except (SecretError, TargetError) as exc:
            return {"status": "deferred",
                    "detail": f"stored; the calendar could not be reached ({exc}), "
                              "so the next poll applies it"}
        return {"status": "error" if report.errors else "ok", **(
            {"errors": report.errors} if report.errors else {})}

    def set_kid_absent(conn, caller, state, child_id: str, absent: bool, note):
        """One kid of a shared team not going to one event, or going again.

        The title drops or regains the kid at once; the event comes off only
        when nobody is left, through the same `withheld.enforce`. On a merged
        event the absence is recorded on that kid's own team copy, and the merge
        step redraws the event without them.
        """
        shown, members = state, []
        if repo.get_source(conn, state.source_id).kind == "merged":
            members = repo.members_of(conn, state.uid)
            kids_here = sorted({k for m in members for k in (m["kids"] or "").split(",") if k})
            holder = next((m for m in members
                           if child_id in (m["kids"] or "").split(",")), None)
            if holder is None:
                raise Problem(422, "invalid", "nothing was changed",
                              errors=[{"field": "child", "code": "not_on_team",
                                       "detail": "that kid is not on a team at this meet",
                                       "candidates": kids_here}])
            state = repo.event_state(conn, holder["member_uid"])
        source = repo.get_source(conn, state.source_id)
        activity = repo.get_activity(conn, source.activity_id)
        kids = {c.id: c for c in repo.team_children(conn, activity)}
        if shown is not state:
            kids = {k: None for k in kids_here}
        if child_id not in kids:
            raise Problem(422, "invalid", "nothing was changed",
                          errors=[{"field": "child", "code": "not_on_team",
                                   "detail": "that kid is not on this event's team",
                                   "candidates": sorted(kids)}])
        if absent and not retire.upcoming(state.shown_starts_at, clock()):
            raise Problem(409, "already_started",
                          "that event has started; taking it off would remove the "
                          "record of something that happened")
        was = child_id in repo.absences(conn, source.id).get(state.uid, set())
        if was != absent:
            repo.set_absence(conn, state.uid, child_id, absent, actor=caller.label)
            repo.log_admin(conn, caller.label, "kid_absent" if absent else "kid_back",
                           f"event:{state.uid}", {"child": child_id, "note": note})
            conn.commit()
            merge_errors: list[str] = []
            try:
                target = write_target or targeting.build_target(conn, secrets=secrets)
                withheld_mod.enforce(conn, source.id, target)
                conn.commit()
                merge_errors = merging.publish_safely(
                    conn, target, now=clock(), secrets=secrets).errors
            except (SecretError, TargetError):
                pass
            result = sync(source) if source.enabled else {"status": "deferred"}
            when = (datetime.fromisoformat(state.shown_starts_at)
                    .astimezone(ZoneInfo(activity.tz)).strftime("%a %-d %b %H:%M"))
            name = repo.get_child(conn, child_id).name
            alert(conn, f"{caller.label} marked {name} "
                        + ("not attending " if absent else "going to ")
                        + f"{activity.name} on {when}" + (f": {note}" if note else "") + ".")
        else:
            result, merge_errors = None, []
        away = set().union(*repo.absences_of(
            conn, [state.uid, *(m["member_uid"] for m in members)]).values())
        fresh = repo.event_state(conn, shown.uid)
        return _dump({"uid": shown.uid, "child": child_id,
                      "status": "not_attending" if absent else None,
                      "going": [k for k in kids if k not in away],
                      "on_calendar": not fresh.cancelled,
                      **({"sync": result} if result else {"detail": "no change"}),
                      **({"merge_errors": merge_errors} if merge_errors else {})})

    def alert(conn, message: str) -> None:
        """Push an admin change that can affect a live calendar.

        Best effort: the change is already saved, so a failed push is only logged.
        """
        config = notify.load(conn)
        if not config.available(secrets):
            return
        try:
            notify_sender(config, secrets, message, title="calsync: admin change")
        except notify.NotifyError as exc:
            log(f"inbound admin: push failed: {exc}")

    def team_name(conn, source: repo.Source) -> str:
        return repo.get_activity(conn, source.activity_id).name

    def event_alert(conn, caller, state, status, note) -> None:
        activity = repo.get_activity(conn, repo.get_source(conn, state.source_id).activity_id)
        when = (datetime.fromisoformat(state.shown_starts_at)
                .astimezone(ZoneInfo(activity.tz)).strftime("%a %-d %b %H:%M"))
        what = (f"marked {activity.name} on {when} {withheld_mod.REASONS[status]}"
                if status else f"put back {activity.name} on {when}")
        alert(conn, f"{caller.label} {what}" + (f": {note}" if note else "") + ".")

    # --- admin: venues -----------------------------------------------------------------

    def venue_body(v: repo.VenueDetail) -> dict:
        return {"id": v.id, "name": v.name, "short_name": v.short_name,
                "address": v.address, "aliases": list(v.aliases),
                "home_to": list(v.home_to)}

    def admin_write(conn) -> repo.ApiKey:
        caller = authorise_admin(conn)
        throttle(f"admin:{caller.id}")
        return caller

    def alias_taken(conn, alias: str, venue_id: int | None) -> None:
        owner = repo.alias_owner(conn, alias)
        if owner is not None and owner != venue_id:
            raise Problem(409, "alias_taken", f"{alias!r} already means another venue",
                          venue=venue_body(repo.get_venue_detail(conn, owner)))

    @app.get(ADMIN + "/venues")
    def list_venues_():
        """Every venue, or those whose name or an alias contains q."""
        q = (request.query.getunicode("q") or "").strip().casefold()
        with connect() as conn:
            authorise_admin(conn)
            venues = [venue_body(v) for v in repo.venues_detailed(conn)
                      if not q or any(q in n.casefold() for n in (v.name, *v.aliases))]
        return _dump({"count": len(venues), "venues": venues})

    @app.post(ADMIN + "/venues")
    def create_venue():
        """A new venue. A name or alias another venue already has is refused."""
        with connect() as conn:
            caller = admin_write(conn)
        try:
            venue = admin.validate_venue(body())
        except inbound.Invalid as exc:
            raise Problem(422, "invalid", "the venue was not created",
                          errors=exc.errors) from None
        with lock_for("admin"), transaction() as conn:
            existing = repo.venue_named(conn, venue["name"])
            if existing is not None:
                raise Problem(409, "already_exists", "a venue has that name",
                              venue=venue_body(repo.get_venue_detail(conn, existing)))
            for alias in [venue["name"], *venue["aliases"]]:
                alias_taken(conn, alias, None)
            venue_id = repo.upsert_venue(conn, name=venue["name"],
                                         short_name=venue["short_name"],
                                         address=venue["address"], commit=False)
            for alias in venue["aliases"]:
                repo.add_venue_alias(conn, venue_id, alias, commit=False)
            repo.log_admin(conn, caller.label, "venue_created", f"venue:{venue_id}",
                           {"name": venue["name"]})
            conn.commit()
            alert(conn, f"{caller.label} added the venue {venue['name']}"
                        + (f" (also {', '.join(venue['aliases'])})" if venue["aliases"]
                           else "") + ".")
            return _dump(venue_body(repo.get_venue_detail(conn, venue_id)), 201)

    def venue_path_id(raw: str) -> int:
        # ASCII digits only: str.isdigit() also takes ² and ١, and int() reads ١ as 1.
        if not re.fullmatch(r"[0-9]{1,18}", raw):
            raise Problem(404, "no_such_venue", "no venue has that id")
        return int(raw)

    @app.post(ADMIN + "/venues/<venue_id>/aliases")
    def add_alias(venue_id):
        """Say that a name a feed uses means this venue. Repeating it is harmless."""
        venue_id = venue_path_id(venue_id)
        with connect() as conn:
            caller = admin_write(conn)
        try:
            alias = admin.validate_alias(body())
        except inbound.Invalid as exc:
            raise Problem(422, "invalid", "nothing was changed", errors=exc.errors) from None
        with lock_for("admin"), transaction() as conn:
            if repo.get_venue_detail(conn, venue_id) is None:
                raise Problem(404, "no_such_venue", "no venue has that id")
            alias_taken(conn, alias, venue_id)
            if repo.alias_owner(conn, alias) is None:
                repo.log_admin(conn, caller.label, "venue_alias_added", f"venue:{venue_id}",
                               {"alias": alias})
                repo.add_venue_alias(conn, venue_id, alias, commit=False)
                conn.commit()
                alert(conn, f"{caller.label}: \"{alias}\" now means "
                            f"{repo.get_venue_detail(conn, venue_id).name}.")
            return _dump(venue_body(repo.get_venue_detail(conn, venue_id)))

    @app.delete(ADMIN + "/venues/<venue_id>/aliases")
    def remove_alias(venue_id):
        """Undo an alias. Removing one the venue does not have is harmless."""
        venue_id = venue_path_id(venue_id)
        with connect() as conn:
            caller = admin_write(conn)
        try:
            alias = admin.validate_alias(body())
        except inbound.Invalid as exc:
            raise Problem(422, "invalid", "nothing was changed", errors=exc.errors) from None
        with lock_for("admin"), transaction() as conn:
            venue = repo.get_venue_detail(conn, venue_id)
            if venue is None:
                raise Problem(404, "no_such_venue", "no venue has that id")
            if alias.casefold() == venue.name.casefold():
                raise Problem(409, "own_name", "that is the venue's own name; rename it "
                                               "in the console")
            if repo.alias_owner(conn, alias) == venue_id:
                conn.execute("DELETE FROM venue_aliases WHERE venue_id = ? "
                             "AND alias = ? COLLATE NOCASE", (venue_id, alias))
                repo.log_admin(conn, caller.label, "venue_alias_removed",
                               f"venue:{venue_id}", {"alias": alias})
                conn.commit()
                alert(conn, f"{caller.label}: \"{alias}\" no longer means {venue.name}.")
            return _dump(venue_body(repo.get_venue_detail(conn, venue_id)))

    @app.put(ADMIN + "/teams/<source_id>/home-venue")
    def set_home_venue(source_id):
        """A team's home ground, which is how calsync tells home games from away."""
        with connect() as conn:
            caller = admin_write(conn)
        try:
            venue_id = admin.validate_home(body())
        except inbound.Invalid as exc:
            raise Problem(422, "invalid", "nothing was changed", errors=exc.errors) from None
        with transaction() as conn:
            source = admin_source(conn, source_id)
            if venue_id is not None and repo.get_venue_detail(conn, venue_id) is None:
                raise Problem(404, "no_such_venue", "no venue has that id")
            repo.set_home_venue(conn, source.activity_id, venue_id)
            repo.log_admin(conn, caller.label, "team_home_venue", f"team:{source_id}",
                           {"venue_id": venue_id})
            conn.commit()
            activity = repo.get_activity(conn, source.activity_id)
            if not source.staging_collection:
                alert(conn, f"{caller.label} set {activity.name}'s home venue to "
                            f"{activity.home_venue}." if venue_id else
                            f"{caller.label} cleared {activity.name}'s home venue.")
            return _dump({"source_id": source_id, "home_venue_id": venue_id,
                          "home_venue": activity.home_venue})

    return app


class _Handler(_QuietHandler):
    timeout = SOCKET_TIMEOUT_S


def serve(app, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    """Run the inbound API. Loopback unless told otherwise, like everything here;
    the compose service binds it for a firewall rule to narrow."""
    with make_server(
        host, port, app, server_class=_Threaded, handler_class=_Handler
    ) as httpd:
        print(f"calsync inbound API on http://{host}:{port}{PREFIX}", flush=True)
        httpd.serve_forever()
