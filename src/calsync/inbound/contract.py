"""What the inbound API is, written once and rendered three ways.

``GET /inbound/v1`` (JSON, for a tool deciding what to call), ``GET
/inbound/v1/docs`` (HTML, for a person) and ``GET /inbound/v1/openapi.json``
(for an assistant platform that turns a spec into callable tools) are all
generated from the tables below. A test holds :data:`ROUTES` against the app's
route table in both directions — the read API's rule, for the read API's
reason: a description that lies is worse than none, because whatever trusts it
fails in a way nobody can reproduce.

Nothing here knows a key or a real source id. The page needs no key to read,
so every example carries placeholders.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field

from . import events as inbound

PREFIX = "/inbound/v1"
KEY_HEADER = "X-Calsync-Key"

#: The worked examples, used by the page and the spec alike.
PRACTICE = {
    "type": "practice",
    "label": "Swim",
    "start": "2026-10-20T17:45:00",
    "end": "2026-10-20T19:30:00",
    "location": "Wrenfield Aquatic Center",
    "rrule": "FREQ=WEEKLY;BYDAY=TU;UNTIL=20270531T235959Z",
    "exdates": ["2026-11-24"],
    "notes": "Arrive 10–15 min early",
}
#: A meet: announced all-day, then replaced by the sessions that point at it.
MEET = {
    "type": "game",
    "label": "Harbor Invitational",
    "start": "2026-11-14",
    "end": "2026-11-15",
    "location": "Ridgeway Natatorium",
}
WARMUP = {
    "type": "practice",
    "label": "Harbor Inv · S3 warm-up",
    "start": "2026-11-14T12:00:00",
    "end": "2026-11-14T12:50:00",
    "location": "Ridgeway Natatorium",
    "part_of": "harbor-invitational",
}
SESSION = {
    "type": "game",
    "label": "Harbor Inv · Session 3",
    "start": "2026-11-14T13:00:00",
    "end": "2026-11-14T16:30:00",
    "location": "Ridgeway Natatorium",
    "notes": "Events:\n- #14 50 Free · heat 3 lane 5 · est. 1:40 pm\n- #22 100 Back · est. 2:30 pm",
    "part_of": "harbor-invitational",
}
OVERRIDE = {"start": "2026-10-27T18:30:00", "location": "Larkmoor Pool"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str          # JSON Schema type
    summary: str
    required: str = ""  # "" | "always" | a condition, in words
    enum: tuple = ()
    example: object = None


#: Every field `events.validate_event` reads, in the order a person writes them.
FIELDS: tuple[Field, ...] = (
    Field("type", "string", "What kind of event. Decides the calendar it lands in "
          "and which alarm it gets.", "always", enum=inbound.TYPES, example="practice"),
    Field("start", "string", "Local wall-clock ISO 8601 in tz, e.g. 2026-10-20T17:45:00. "
          "An offset is accepted and converted. A bare date (2026-11-14) is an "
          "all-day event.", "always", example="2026-10-20T17:45:00"),
    Field("end", "string", "Same form as start, after it. For an all-day event, the "
          "last day (inclusive), and optional.", "timed events",
          example="2026-10-20T19:30:00"),
    Field("tz", "string", "IANA zone. Defaults to the team's.", example="America/New_York"),
    Field("label", "string", "The short word in the title — Swim, Dryland, Meet.",
          example="Swim"),
    Field("location", "string", "Free text. Resolved through calsync's venue table "
          "like any feed's.", example="Wrenfield Aquatic Center"),
    Field("opponent", "string", "Games only.", example="Ridgeway Sharks"),
    Field("home", "boolean", "Games only. Leave it out when unknown — calsync never "
          "guesses home or away.", example=False),
    Field("notes", "string", "Into the event body.", example="Arrive 10–15 min early"),
    Field("url", "string", "Into the event body.", example="https://example.org/meet"),
    Field("rrule", "string", "A repeating series: FREQ=DAILY|WEEKLY with INTERVAL, "
          "BYDAY, WKST=MO and exactly one of UNTIL or COUNT. calsync writes one event "
          "per date. There is no default season end.",
          example="FREQ=WEEKLY;BYDAY=TU;UNTIL=20270531T235959Z"),
    Field("exdates", "array", "Dates the series skips, YYYY-MM-DD.",
          example=["2026-11-24"]),
    Field("part_of", "string", "The external_id of the event this is part of — a "
          "meet's session or warm-up names the meet. While any live part points at "
          "it, the whole meet comes off the calendar (every day of it, not only the "
          "days with parts), and it comes back if every part is cancelled. Send a "
          "swimmer's parts once their full schedule is known. Parts never count as "
          "duplicates of their meet. One level only.", example="harbor-invitational"),
)
assert {f.name for f in FIELDS} == inbound.FIELDS, "contract and validator disagree"

OVERRIDE_FIELDS: tuple[Field, ...] = (
    Field("start", "string", "New local start for this date. End keeps the series "
          "duration unless given.", example="2026-10-27T18:30:00"),
    Field("end", "string", "New local end for this date."),
    Field("location", "string", "Where this date is instead.", example="Larkmoor Pool"),
    Field("notes", "string", "Notes for this date only."),
    Field("label", "string", "Label for this date only."),
)
assert {f.name for f in OVERRIDE_FIELDS} == inbound.OVERRIDE_FIELDS


@dataclass(frozen=True)
class Param:
    name: str
    where: str          # path | query | header
    summary: str
    required: bool = False
    example: str | None = None


P = {
    "source_id": Param("source_id", "path", "The team, from the console's key page.",
                       True, "in-herons"),
    "external_id": Param("external_id", "path", "Your id for the event: 1–128 of "
                         "A-Z a-z 0-9 . _ -. Chosen by you, so a retried create cannot "
                         "make a second event.", True, "tue-swim"),
    "date": Param("date", "path", "One date of the series, YYYY-MM-DD.", True, "2026-10-27"),
    "from": Param("from", "query", "First local date to include, YYYY-MM-DD.",
                  example="2026-10-27"),
    "to": Param("to", "query", "Last local date to include, YYYY-MM-DD.",
                example="2026-10-27"),
    "allow_overlap": Param("allow_overlap", "query", "true to create an event that "
                           "overlaps a live one of the same type on purpose.",
                           example="true"),
    "If-Match": Param("If-Match", "header", "Write only if the event's ETag still "
                      "matches; 412 otherwise."),
    "If-None-Match": Param("If-None-Match", "header", "* makes a PUT create-only; 412 "
                           "if the id was already used.", example="*"),
}


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    op: str
    summary: str
    detail: str = ""
    params: tuple[str, ...] = ()
    body: str | None = None          # "Event" | "Override"
    example: dict | None = None
    responses: dict = field(default_factory=dict)   # status -> (words, schema|None)
    auth: bool = True


_E = "Error"
ROUTES: tuple[Route, ...] = (
    Route("GET", PREFIX, "describe", "This description, as JSON.",
          "Needs no key. Links to the page and the OpenAPI spec.",
          responses={200: ("The description", None)}, auth=False),
    Route("GET", PREFIX + "/docs", "docs", "This description, as a page.",
          responses={200: ("HTML", None)}, auth=False),
    Route("GET", PREFIX + "/openapi.json", "openapi", "OpenAPI 3.1 spec.",
          "Import it into an assistant platform to get these calls as tools.",
          responses={200: ("The spec", None)}, auth=False),
    Route("GET", PREFIX + "/sources/{source_id}", "getSource",
          "Which team this key writes to.",
          "Child, sport, timezone, and whether it is still on the staging calendar. "
          "A cheap first call to prove the key and the proxy both work.",
          params=("source_id",),
          responses={200: ("The team", "Source"), 401: ("Wrong or missing key", _E)}),
    Route("GET", PREFIX + "/sources/{source_id}/events", "listEvents",
          "Every event sent for this team.",
          "Cancelled ones included, marked. With from and to, only events with a "
          "date in that window, carrying only those dates. **List first, then write "
          "by external_id** — you are not expected to remember ids.",
          params=("source_id", "from", "to"),
          responses={200: ("The events", "EventList"), 422: ("Bad window", _E)}),
    Route("GET", PREFIX + "/sources/{source_id}/events/{external_id}", "getEvent",
          "One event, with every date it produces.",
          params=("source_id", "external_id"),
          responses={200: ("The event", "StoredEvent"), 404: ("Never sent", _E)}),
    Route("PUT", PREFIX + "/sources/{source_id}/events/{external_id}", "putEvent",
          "Create or replace an event.",
          "Sending the same body again changes nothing. A new id that overlaps a live "
          "event of the same type on the same day is refused as a possible duplicate "
          "— update that event instead, or resend with allow_overlap=true. A part "
          "never counts as a duplicate of the event it names in part_of.",
          params=("source_id", "external_id", "allow_overlap", "If-Match", "If-None-Match"),
          body="Event", example=PRACTICE,
          responses={201: ("Created", "StoredEvent"), 200: ("Replaced or unchanged", "StoredEvent"),
                     409: ("possible_duplicate, or the team is paused", _E),
                     412: ("ETag mismatch or already_exists", _E),
                     422: ("Invalid; every field problem listed", _E),
                     429: ("More than 60 writes a minute", _E)}),
    Route("DELETE", PREFIX + "/sources/{source_id}/events/{external_id}", "cancelEvent",
          "Cancel an event, every date of it.",
          "Comes off the calendar. Idempotent. PUT it again to bring it back.",
          params=("source_id", "external_id", "If-Match"),
          responses={204: ("Cancelled, or already was", None), 404: ("Never sent", _E)}),
    Route("PUT", PREFIX + "/sources/{source_id}/events/{external_id}/occurrences/{date}",
          "putOccurrence", "Change one date of a series.",
          "Only start, end, location, notes and label. Brings the date back if it "
          "had been cancelled.",
          params=("source_id", "external_id", "date", "If-Match"),
          body="Override", example=OVERRIDE,
          responses={200: ("Changed", "StoredEvent"),
                     422: ("Not a date the series has", _E)}),
    Route("DELETE", PREFIX + "/sources/{source_id}/events/{external_id}/occurrences/{date}",
          "cancelOccurrence", "Cancel one date of a series.",
          "Idempotent. PUT the occurrence to bring it back.",
          params=("source_id", "external_id", "date", "If-Match"),
          responses={204: ("Cancelled, or already was", None),
                     422: ("Not a date the series has", _E)}),
)


# --- JSON --------------------------------------------------------------------------


def describe() -> dict:
    return {
        # No version: the one page served without a key, and which build is
        # running is worth something only to an attacker.
        "name": "calsync inbound",
        "docs": PREFIX + "/docs",
        "openapi": PREFIX + "/openapi.json",
        "auth": {"header": KEY_HEADER,
                 "detail": "one key per team, made in the console; Authorization is "
                           "never read and is left to any proxy in front"},
        "flow": "list events (GET …/events?from=&to=), match the one to change, "
                "write to its external_id",
        "routes": [{"method": r.method, "path": r.path, "summary": r.summary,
                    "detail": r.detail} for r in ROUTES],
        "event": {f.name: f.summary for f in FIELDS},
        "not_accepted": sorted(inbound._REFUSED),
    }


# --- OpenAPI -----------------------------------------------------------------------


def _schema(fields: tuple[Field, ...], *, required=()) -> dict:
    props = {}
    for f in fields:
        prop: dict = {"type": f.type, "description": f.summary}
        if f.type == "array":
            prop["items"] = {"type": "string", "format": "date"}
        if f.enum:
            prop["enum"] = list(f.enum)
        if f.example is not None:
            prop["example"] = f.example
        props[f.name] = prop
    out = {"type": "object", "additionalProperties": False, "properties": props}
    if required:
        out["required"] = list(required)
    return out


def openapi(server: str) -> dict:
    occurrence = {"type": "object", "properties": {
        "date": {"type": "string", "format": "date"},
        "uid": {"type": "string"},
        "start": {"type": "string", "format": "date-time"},
        "end": {"type": "string", "format": "date-time"},
        "all_day": {"type": "boolean"}}}
    stored = {"type": "object", "properties": {
        "source_id": {"type": "string"},
        "external_id": {"type": "string"},
        "etag": {"type": "string"},
        "cancelled": {"type": "boolean", "description": "The sender cancelled it."},
        "superseded_by": {"type": "array", "items": {"type": "string"},
                          "description": "Live parts that have replaced this event "
                                         "on the calendar (part_of)."},
        "on_calendar": {"type": "boolean", "description": "Whether calsync keeps this "
                        "event on a calendar: not cancelled and not superseded."},
        "warnings": {"type": "array", "items": {"type": "string"},
                     "description": "On writes: accepted, but worth a look."},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "event": {"$ref": "#/components/schemas/Event"},
        "overrides": {"type": "object",
                      "additionalProperties": {"$ref": "#/components/schemas/Override"}},
        "occurrences": {"type": "array", "items": occurrence},
        "changed": {"type": "boolean", "description": "On writes: false if nothing changed."},
        "orphaned_overrides": {"type": "array", "items": {"type": "string"},
                               "description": "Dates whose override was dropped because "
                                              "the series no longer has them."},
        "sync": {"type": "object", "description": "What reaching the calendar did: "
                 "status ok, held, error, or deferred (stored; the next poll applies it)."},
    }}
    error = {"type": "object", "properties": {"error": {"type": "object", "properties": {
        "code": {"type": "string"},
        "detail": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "object", "properties": {
            "field": {"type": "string"}, "code": {"type": "string"},
            "detail": {"type": "string"}}}},
        "duplicates": {"type": "array", "items": {"type": "object", "properties": {
            "external_id": {"type": "string"}, "date": {"type": "string"}}}},
    }}}}
    paths: dict = {}
    for r in ROUTES:
        op: dict = {"operationId": r.op, "summary": r.summary}
        if r.detail:
            op["description"] = r.detail
        if r.params:
            op["parameters"] = [
                {"name": P[n].name, "in": P[n].where, "required": P[n].required,
                 "description": P[n].summary, "schema": {"type": "string"},
                 **({"example": P[n].example} if P[n].example else {})}
                for n in r.params
            ]
        if r.body:
            content = {"schema": {"$ref": f"#/components/schemas/{r.body}"}}
            if r.example:
                content["example"] = r.example
            op["requestBody"] = {"required": True,
                                 "content": {"application/json": content}}
        op["responses"] = {
            str(code): ({"description": words} if schema is None else
                        {"description": words, "content": {"application/json": {
                            "schema": {"$ref": f"#/components/schemas/{schema}"}}}})
            for code, (words, schema) in r.responses.items()
        }
        if r.auth:
            op["responses"].setdefault("401", {"description": "Wrong or missing key"})
        else:
            op["security"] = []
        paths.setdefault(r.path, {})[r.method.lower()] = op

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "calsync inbound",
            "version": "1",
            "description": "Send one team's events to calsync, which writes them to the "
                           "family calendar. List first, match, then write by "
                           "external_id. The title and calendar are calsync's to decide.",
        },
        "servers": [{"url": server}],
        # Either both (through the proxy) or the key alone (on the LAN).
        "security": [{"calsyncKey": [], "proxyBasic": []}, {"calsyncKey": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "calsyncKey": {"type": "apiKey", "in": "header", "name": KEY_HEADER,
                               "description": "The team's key from the console."},
                "proxyBasic": {"type": "http", "scheme": "basic",
                               "description": "The authenticating proxy's own login, "
                                              "e.g. Pangolin Basic Header Auth. Never "
                                              "read by calsync."},
            },
            "schemas": {
                "Event": _schema(FIELDS, required=("type", "start")),
                "Override": _schema(OVERRIDE_FIELDS),
                "StoredEvent": stored,
                "EventList": {"type": "object", "properties": {
                    "source_id": {"type": "string"}, "count": {"type": "integer"},
                    "events": {"type": "array",
                               "items": {"$ref": "#/components/schemas/StoredEvent"}}}},
                "Source": {"type": "object", "properties": {
                    k: {"type": t} for k, t in (
                        ("source_id", "string"), ("team", "string"), ("sport", "string"),
                        ("child", "string"), ("tz", "string"), ("enabled", "boolean"),
                        ("staged_to", "string"), ("last_push_at", "string"))}},
                "Error": error,
            },
        },
    }


# --- HTML ----------------------------------------------------------------------------


def _curl(server: str, r: Route) -> str:
    path = r.path
    for name in ("source_id", "external_id", "date"):
        path = path.replace("{" + name + "}", P[name].example)
    if "from" in r.params:
        path += "?from=2026-10-27&to=2026-10-27"
    lines = [f"curl -X {r.method} '{server}{path}' \\"]
    if r.auth:
        lines.append('  -u "$PROXY_USER:$PROXY_PASS" \\')
        lines.append(f'  -H "{KEY_HEADER}: $CALSYNC_KEY"' + (" \\" if r.body else ""))
    if r.body:
        lines.append("  -H 'Content-Type: application/json' \\")
        lines.append("  -d '" + json.dumps(r.example, indent=2, ensure_ascii=False)
                     .replace("'", "'\\''") + "'")
    if not r.auth:
        lines[-1] = lines[-1].rstrip(" \\")
    return "\n".join(lines)


def _rows(fields: tuple[Field, ...]) -> str:
    e = html.escape
    out = []
    for f in fields:
        kind = " | ".join(f.enum) if f.enum else f.type
        out.append(f"<tr><td><code>{e(f.name)}</code></td><td>{e(kind)}</td>"
                   f"<td>{e(f.required or '')}</td><td>{e(f.summary)}</td></tr>")
    return "\n".join(out)


def page(server: str) -> str:
    e = html.escape
    routes = []
    for r in ROUTES:
        if not r.auth:
            continue
        params = "".join(
            f"<li><code>{e(P[n].name)}</code> <span class=where>{P[n].where}</span> "
            f"{e(P[n].summary)}</li>" for n in r.params if P[n].where != "path")
        statuses = " ".join(
            f"<span class=status title='{e(words)}'>{code}</span>"
            for code, (words, _s) in sorted(r.responses.items()))
        detail = e(r.detail).replace("**List first, then write by external_id**",
                                     "<strong>List first, then write by external_id</strong>")
        routes.append(f"""
<section class=route id="{r.op}">
  <h3><span class="m m-{r.method.lower()}">{r.method}</span> <code>{e(r.path)}</code></h3>
  <p class=summary>{e(r.summary)}</p>
  {f'<p>{detail}</p>' if r.detail else ''}
  {f'<ul class=params>{params}</ul>' if params else ''}
  <p class=statuses>{statuses}</p>
  <pre><code>{e(_curl(server, r))}</code></pre>
</section>""")

    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=referrer content=no-referrer>
<title>calsync inbound API</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--card:#fff;--code:#f3f1ea;
--get:#2f6f4f;--put:#8a5a12;--delete:#9b2c2c;--accent:#2b5c8a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151513;--fg:#ecebe6;--muted:#a3a29b;
--line:#2e2d29;--card:#1d1c1a;--code:#24231f;--get:#7cc39c;--put:#e0b066;--delete:#ef8a8a;
--accent:#8cb8e0}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 ui-sans-serif,system-ui,
-apple-system,"Segoe UI",sans-serif}}
main{{max-width:52rem;margin:0 auto;padding:2.5rem 1rem 4rem}}
h1{{font-size:1.9rem;margin:0 0 .3rem;letter-spacing:-.01em}}
h2{{font-size:1.15rem;margin:2.6rem 0 .8rem;padding-top:1rem;border-top:1px solid var(--line)}}
h3{{font-size:1rem;margin:0 0 .4rem;display:flex;gap:.6rem;align-items:baseline;flex-wrap:wrap}}
.lede{{color:var(--muted);margin:0 0 1rem}}
a{{color:var(--accent)}}
code,pre{{font:13.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}}
:not(pre)>code{{background:var(--code);padding:.08rem .3rem;border-radius:4px;word-break:break-word}}
pre{{background:var(--code);padding:.9rem 1rem;border-radius:8px;overflow-x:auto;margin:.6rem 0 0}}
.route,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1rem 1.1rem;margin:0 0 1rem}}
.route p{{margin:.35rem 0}}
.summary{{font-weight:600}}
.m{{font:700 12px ui-monospace,monospace;padding:.15rem .45rem;border-radius:4px;color:var(--card)}}
.m-get{{background:var(--get)}}.m-put{{background:var(--put)}}.m-delete{{background:var(--delete)}}
.status{{display:inline-block;font:12px ui-monospace,monospace;border:1px solid var(--line);
border-radius:4px;padding:0 .35rem;margin-right:.25rem;color:var(--muted)}}
.params{{margin:.4rem 0;padding-left:1.1rem;color:var(--muted)}}
.where{{font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
table{{width:100%;border-collapse:collapse;font-size:14.5px}}
td,th{{text-align:left;vertical-align:top;padding:.45rem .5rem;border-bottom:1px solid var(--line)}}
th{{color:var(--muted);font-weight:500}}
.wrap{{overflow-x:auto}}
ol li,ul li{{margin:.25rem 0}}
</style></head><body><main>
<h1>calsync inbound API</h1>
<p class=lede>Send one team's events to calsync. It writes them to the family's calendar
with its own titles, calendars, alarms and warm-ups — you describe events, it words them.
Also as <a href="{PREFIX}">JSON</a> and <a href="{PREFIX}/openapi.json">OpenAPI 3.1</a>.</p>

<h2>Auth</h2>
<div class=card>
<p><code>{KEY_HEADER}: &lt;key&gt;</code> — the team's key, shown once in the console when
the team is created. One key opens one team.</p>
<p><code>Authorization</code> is never read. It belongs to the proxy in front (Pangolin's
Basic Header Auth: <code>-u user:pass</code>), so the two never collide.</p>
</div>

<h2>The flow</h2>
<div class=card>
<p>You are not expected to remember ids. To change something:</p>
<ol>
<li><code>GET …/events?from=2026-10-27&amp;to=2026-10-27</code> — just that day's events.</li>
<li>Find the one that matches.</li>
<li><code>PUT</code> or <code>DELETE</code> its <code>external_id</code> — or
<code>…/occurrences/2026-10-27</code> for that one date of a series.</li>
</ol>
<p>Creating: pick an id that describes the event (<code>tue-swim</code>). Sending the
same body again changes nothing, so a retry is always safe.</p>
</div>

<h2>Safeguards</h2>
<div class="card wrap"><table>
<tr><th>409 <code>possible_duplicate</code></th><td>A new id that overlaps a live event of
the same type on the same day. The response names the existing id — update that instead,
or resend with <code>?allow_overlap=true</code> if both are real.</td></tr>
<tr><th><code>If-None-Match: *</code></th><td>Create-only. 412 <code>already_exists</code>
if the id was used.</td></tr>
<tr><th><code>If-Match: "&lt;etag&gt;"</code></th><td>412 if the event changed since you
read it.</td></tr>
<tr><th>429</th><td>More than 60 writes a minute for one team.</td></tr>
<tr><th>Staging</th><td>A new team's events go to a staging calendar until a person
promotes it.</td></tr>
</table></div>

<h2>Calls</h2>
<p class=lede>Set <code>CALSYNC_KEY</code>, <code>PROXY_USER</code> and
<code>PROXY_PASS</code>, and replace <code>in-herons</code> with your source id.</p>
{''.join(routes)}

<h2>Event fields</h2>
<div class="card wrap"><table>
<tr><th>Field</th><th>Type</th><th>Required</th><th></th></tr>
{_rows(FIELDS)}
</table>
<p>Refused, with the reason: {', '.join(f'<code>{e(n)}</code>' for n in sorted(inbound._REFUSED))}.
calsync composes the title and chooses the calendar; one source is one team for one
child; cancelling is a <code>DELETE</code>.</p></div>

<h2>Changing one date</h2>
<div class="card wrap"><table>
<tr><th>Field</th><th>Type</th><th></th><th></th></tr>
{_rows(OVERRIDE_FIELDS)}
</table></div>

<h2>A season, start to finish</h2>
<div class=card>
<p>A weekly practice, Thanksgiving week off:</p>
<pre><code>{e(json.dumps(PRACTICE, indent=2, ensure_ascii=False))}</code></pre>
<p>One week moves pools — <code>PUT …/events/tue-swim/occurrences/2026-10-27</code>:</p>
<pre><code>{e(json.dumps(OVERRIDE, indent=2, ensure_ascii=False))}</code></pre>
<p>A meet is announced, all day — <code>PUT …/events/harbor-invitational</code>:</p>
<pre><code>{e(json.dumps(MEET, indent=2, ensure_ascii=False))}</code></pre>
<p>The session schedule comes out. Each session the swimmer is in, and its warm-up,
names the meet in <code>part_of</code>; the all-day meet comes off the calendar on its own
— <code>PUT …/events/harbor-invitational-s3-warmup</code>:</p>
<pre><code>{e(json.dumps(WARMUP, indent=2, ensure_ascii=False))}</code></pre>
<p>— and <code>PUT …/events/harbor-invitational-s3</code>:</p>
<pre><code>{e(json.dumps(SESSION, indent=2, ensure_ascii=False))}</code></pre>
<p>Practice cancelled for snow — <code>DELETE …/events/tue-swim/occurrences/2027-01-19</code>.</p>
</div>

<h2>Errors</h2>
<div class=card>
<p>Always JSON. Field problems are all listed at once:</p>
<pre><code>{e(json.dumps({"error": {"code": "invalid", "detail": "the event was not stored",
               "errors": [{"field": "end", "code": "end_before_start",
                           "detail": "end must be after start"}]}}, indent=2))}</code></pre>
</div>
</main></body></html>
"""
