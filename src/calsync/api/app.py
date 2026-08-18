"""Routes. Thin, like the console's — the work is in `repo.stored_events`."""

from __future__ import annotations

import hmac
import json
import socketserver
from contextlib import closing
from datetime import datetime, timedelta, timezone
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from bottle import Bottle, HTTPResponse, request, response

from .. import db, repo
from ..normalize import title as title_norm
from ..secrets import SecretError, SecretStore
from ..settings import Settings

#: docs/API.md: "Selectors default to a 14-day window and are always bounded."
DEFAULT_WINDOW_DAYS = 14

#: Fields an amendment could plausibly carry, and therefore the ones whose
#: ownership a client has to be able to see. Everything here is currently held
#: by the same contributor — the feed — which is exactly what the block says.
RESOLVED_FIELDS = ("starts_at", "ends_at", "venue", "opponent", "kind", "detail")


class ApiError(HTTPResponse):
    """A refusal a client can act on.

    Structured rather than prose, following the shape docs/API.md already
    specifies for validation errors: a client that has to regex an error message
    to know what to fix will get it wrong.
    """

    def __init__(self, status: int, code: str, detail: str, **extra):
        body = {"error": {"code": code, "detail": detail, **extra}}
        super().__init__(
            body=json.dumps(body, indent=2, sort_keys=True) + "\n",
            status=status,
            headers={"Content-Type": "application/json"},
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(db_path, *, secrets: SecretStore | None = None, clock=None) -> Bottle:
    """Build the read API.

    ``clock`` pins "now", which decides the default window — the same reason the
    console takes one, and the same failure if it does not: a fixture from last
    spring returns nothing and every assertion passes vacuously.

    The bearer token is resolved **once, here**, so a deployment with no token
    configured fails at startup with something actionable rather than serving
    every request as a 500. There is no unauthenticated mode to fall back to.
    """
    app = Bottle()
    secrets = secrets or SecretStore()
    clock = clock or _now
    db_path = str(db_path)

    with closing(db.open_db(db_path)) as conn:
        ref = Settings.load(conn).api_token_ref
    try:
        token = secrets.get(ref)
    except SecretError as exc:
        # Deliberately not "start anyway and refuse every request": a read API
        # for children's schedules that comes up in a state where the operator
        # might not notice it is unusable is worse than one that will not start.
        raise SecretError(f"the API needs a bearer token before it can serve: {exc}") from exc

    def connect():
        return closing(db.connect(db_path))

    # --- auth --------------------------------------------------------------

    @app.hook("before_request")
    def _bearer():
        """Every route, including ones added later.

        No cookies are involved, so there is nothing for another site to ride
        and no `Sec-Fetch-Site` check to make — the console needs one precisely
        because it has no token, and this has the opposite arrangement.
        """
        header = request.get_header("Authorization") or ""
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            raise ApiError(401, "no_credential",
                           "send Authorization: Bearer <token>")
        # Constant-time: the comparison is against a bearer credential, and the
        # early-exit kind leaks it a byte at a time to anything that can time a
        # request.
        if not hmac.compare_digest(presented.strip(), token):
            raise ApiError(401, "bad_credential", "that token is not recognised")

    @app.hook("after_request")
    def _json():
        # Bottle defaults a str return to text/html, and a client that content
        # -sniffs its way to the right parser is a client that breaks silently.
        response.content_type = "application/json"

    # --- reads -------------------------------------------------------------

    @app.get("/v1/events")
    def list_events():
        with connect() as conn:
            settings = Settings.load(conn)
            now = clock()
            window = _window(request.query, now=now, settings=settings)
            stored = repo.stored_events(
                conn,
                start=window["start"].isoformat(),
                end=window["end"].isoformat(),
                # `getunicode`, not `.get`: Bottle decodes as latin-1 by
                # default, which mangles any id carrying an accent.
                child_id=request.query.getunicode("child") or None,
                activity_id=request.query.getunicode("activity") or None,
            )
            body = {
                "from": window["start"].isoformat(),
                "to": window["end"].isoformat(),
                "count": len(stored),
                "events": [_event_json(conn, item, settings) for item in stored],
                "sources": _sources_json(conn, stored, now=now),
            }
            if window["clamped"]:
                # Said out loud rather than silently returning fewer events. A
                # schedule read that quietly omits a day reads as "nothing on",
                # which is the one wrong answer this kind of response can give.
                body["retained_from"] = window["start"].isoformat()
                body["retention_note"] = (
                    "stored content is kept only for the sync window; "
                    f"nothing is retained before {window['start'].isoformat()}"
                )
            return _dump(body)

    @app.get("/v1/events/<uid:path>")
    def get_event(uid):
        with connect() as conn:
            settings = Settings.load(conn)
            item = repo.stored_event(conn, uid)
            if item is None:
                raise ApiError(
                    404, "unknown_event",
                    "no stored event with that uid; it may have been written "
                    "before content was recorded, or aged out of the window",
                    got=uid,
                )
            return _dump(
                {
                    "event": _event_json(conn, item, settings),
                    "sources": _sources_json(conn, [item], now=clock()),
                }
            )

    return app


# --- serialization ----------------------------------------------------------


def _dump(body: dict) -> str:
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def _window(query, *, now: datetime, settings: Settings) -> dict:
    """Resolve the requested span, always bounded.

    Clamped at the bottom to what is actually retained, because content ages out
    with the sync window and a caller asking for last March should be told that
    rather than handed an empty list.
    """
    start = _stamp(query.getunicode("from"), default=now)
    end = _stamp(
        query.getunicode("to"), default=now + timedelta(days=DEFAULT_WINDOW_DAYS)
    )
    if end < start:
        raise ApiError(400, "empty_window", "`to` precedes `from`")

    span = timedelta(
        days=settings.sync_window_back_days + settings.sync_window_forward_days
    )
    if end - start > span:
        raise ApiError(
            400, "window_too_wide",
            f"the widest meaningful span is {span.days} days — nothing is "
            "written outside the sync window",
            got=f"{(end - start).days} days",
        )

    floor = now - timedelta(days=settings.sync_window_back_days)
    clamped = start < floor
    return {"start": max(start, floor), "end": end, "clamped": clamped}


def _stamp(value, *, default: datetime) -> datetime:
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(
            400, "bad_datetime",
            "send RFC3339, or a bare date for midnight UTC", got=value,
        ) from exc
    # A naive bound would shift the window by up to a day, which is the same
    # class of bug `Event.__post_init__` refuses outright.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _event_json(conn, item: repo.StoredEvent, settings: Settings) -> dict:
    event = item.event
    activity = repo.get_activity(conn, item.activity_id)
    children = [repo.get_child(conn, item.child_id)]
    child = children[0]

    venue = None
    if event.venue:
        row = repo.venue_ref(conn, event.venue.name, event.venue.raw)
        venue = {
            "id": row["id"] if row else None,
            "canonical_name": row["canonical_name"] if row else event.venue.name,
            "raw": event.venue.raw,
            "address": event.venue.address,
            # Which field within the place, kept out of the venue's identity so
            # one park does not become one venue per pitch.
            "field": event.venue.field,
            "pin_confirmed": bool(row["pin_confirmed"]) if row else False,
        }

    return {
        "uid": event.uid,
        "child": {"id": child.id, "name": child.name},
        "activity": {"id": activity.id, "name": activity.name, "sport": activity.sport},
        "kind": "game" if event.is_game else "practice",
        "is_game": event.is_game,
        "starts_at": event.starts_at.isoformat(),
        "ends_at": event.ends_at.isoformat(),
        "tz": event.tz,
        "opponent": event.opponent,
        # Tri-state. null means nobody could tell, which is not the same as home:
        # some feeds phrase every fixture as "vs".
        "home": event.home,
        "detail": event.detail,
        "notes": event.body,
        "kit": event.kit,
        "arrive_at": event.arrive_at.isoformat() if event.arrive_at else None,
        "url": event.url,
        "venue": venue,
        "collection": item.collection,
        "cancelled": item.cancelled,
        # Composed now, from the fields above, through the same code the
        # calendar goes through. Never parse it: it is a render, and it changes
        # whenever the naming convention does (docs/NAMING.md).
        "summary_rendered": title_norm.render(event, activity, children, settings),
        "resolution": _resolution(conn, item),
    }


def _resolution(conn, item: repo.StoredEvent) -> dict:
    """Which source owns each field, its tier, and when it said so.

    Uniform today, because a feed is the only contributor to any event. It is
    per-field rather than per-event anyway, so that a client written against it
    keeps working when amendments start holding individual fields at tier 1
    (docs/MATRIX.md §4) — that change should widen what this says, not change
    its shape.
    """
    source = repo.get_source(conn, item.source_id)
    held = {
        "source_id": item.source_id,
        "tier": source.tier if source else None,
        "observed_at": item.observed_at,
    }
    return {field: held for field in RESOLVED_FIELDS}


def _sources_json(conn, items, *, now: datetime) -> list[dict]:
    """Freshness for every source behind the returned events.

    A stored copy lags its feed by up to one poll, exactly as the calendar
    always has. That is fine as long as it is legible — so a source whose last
    poll failed, or which has gone quiet for more than a couple of intervals, is
    named here rather than having its events served as though they were current.
    `digest.py` names unreadable sources for the same reason: silently omitting
    one reads as "nothing on".
    """
    health = repo.source_health(conn)
    out = []
    for source_id in sorted({item.source_id for item in items}):
        row = health.get(source_id)
        if row is None:
            continue
        last = _parse_db_stamp(row["last_success_at"])
        overdue = timedelta(seconds=2 * (row["poll_interval_s"] or 1200))
        out.append(
            {
                "id": source_id,
                "enabled": bool(row["enabled"]),
                "last_success_at": last.isoformat() if last else None,
                "last_error": row["last_error"],
                "stale": last is None or (now - last) > overdue,
            }
        )
    return out


def _parse_db_stamp(value) -> datetime | None:
    """`datetime('now')` writes "YYYY-MM-DD HH:MM:SS", in UTC and unmarked."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- server -----------------------------------------------------------------


class _Threaded(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        """Do not log request lines.

        A query string here carries a child's id and the dates somebody asked
        about, and an Authorization header is one misconfiguration away from a
        log file. The poller already reports what matters.
        """


def serve(app, *, host: str = "127.0.0.1", port: int = 8731) -> None:
    """Run the API.

    Loopback by default and it should stay that way. The token is a real
    control, unlike the console's absent login, but it is one control in front
    of children's names, schedules and physical locations — put the same VPN or
    proxy in front of this that the console is behind rather than binding it to
    0.0.0.0 and calling the token sufficient.
    """
    with make_server(
        host, port, app, server_class=_Threaded, handler_class=_QuietHandler
    ) as httpd:
        print(f"calsync API on http://{host}:{port}/v1", flush=True)
        httpd.serve_forever()
