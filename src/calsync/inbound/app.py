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
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from wsgiref.simple_server import make_server

from zoneinfo import ZoneInfo

from bottle import Bottle, HTTPResponse, request, response

from .. import db, repo, sources, targeting
from ..api.app import _QuietHandler, _Threaded
from ..secrets import SecretError, SecretStore
from ..settings import Settings
from ..sync import sync_source
from ..targets import TargetError
from . import contract
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


def _unauthorised() -> Problem:
    return Problem(
        401, "bad_credential",
        f"send this source's key in {KEY_HEADER}; Authorization is not read",
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
               monotonic=time.monotonic) -> Bottle:
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
            raise _unauthorised()
        touch(conn, source_id)
        return source

    def touch(conn, source_id: str) -> None:
        """Record that the key was used, at most once a minute.

        The console shows the last *write*; a key used only to read the
        schedule would otherwise leave no trace, and reading is what a leaked
        key is for. Best effort: a busy database costs the timestamp, never
        the request.
        """
        now = clock()
        last = repo.inbound_key_used_at(conn, source_id)
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=1):
            return
        # A short wait of its own: with the connection's normal timeout, a
        # poller holding the database would stall an ordinary GET for seconds
        # over a timestamp that is allowed to be skipped.
        conn.execute(f"PRAGMA busy_timeout = {TOUCH_TIMEOUT_MS}")
        try:
            repo.touch_inbound_key(conn, source_id, now.isoformat())
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
        return out

    def render_item(conn, source: repo.Source, item: repo.InboundItem,
                    window=None) -> dict:
        tz = activity_tz(conn, source)
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
            rendered = [render_item(conn, source, i, window)
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
                state, orphaned = inbound.put_event(
                    source.id, external_id, old.state if old else None, event,
                    default_tz=tz)
                if old is None and request.query.get("allow_overlap") not in ("1", "true"):
                    others = [(i.external_id, i.state) for i in items
                              if not i.cancelled]
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
                    out.update(changed=False, orphaned_overrides=[])
                    return _dump(out, headers={"ETag": f'"{old.etag}"'})
                item = store(conn, source, external_id, state, cancelled=False)
            result = sync(source)
        with connect() as conn:
            out = render_item(conn, source, item)
        out.update(changed=True, orphaned_overrides=orphaned, sync=result)
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
