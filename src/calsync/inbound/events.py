"""What a sender may say about an event, and what calsync makes of it.

Pure: dicts in, dicts out, no database and no clock. `app.py` validates with
this, stores the result, and `sources/inbound.py` turns stored items into
`Event`s with :func:`instances`.

A stored item is a ``state`` dict::

    {"event": {...validated body...},
     "overrides": {"2026-10-27": {...}},      # per-occurrence changes
     "retired": [{"uid", "start", "end", "all_day"}, ...]}

``retired`` is what makes per-event updates safe without a full snapshot. When
a change stops an occurrence from being generated — an exdate, an UNTIL brought
forward, a series moved from Tuesdays to Wednesdays — the occurrence it used to
generate is remembered here, and the adapter emits it as cancelled. Without it
those events would simply vanish from what the adapter returns, which is an
absence, which is exactly the signal calsync distrusts: four of them and the
disappearance guard holds the poll.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets as _secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import zones
from . import recurrence

TYPES = ("practice", "game")

#: What the sender may send. Everything else is refused, by name, below.
FIELDS = {
    "type", "label", "start", "end", "tz", "location", "opponent", "home",
    "notes", "url", "rrule", "exdates", "part_of",
}
OVERRIDE_FIELDS = {"start", "end", "location", "notes", "label"}

#: Fields a sender is likely to try, and why calsync does not take them. A bare
#: "unknown field" teaches nothing; these say where the decision lives instead.
_REFUSED = {
    "title": "the title is composed by calsync from type, label and opponent "
             "using the household's naming template",
    "summary": "the title is composed by calsync from type, label and opponent "
               "using the household's naming template",
    "calendar": "the calendar is chosen by calsync's routing from the team and type",
    "collection": "the calendar is chosen by calsync's routing from the team and type",
    "child": "the source belongs to one team for one child; onboard another for another",
    "swimmers": "the source belongs to one team for one child; onboard another for another",
    "team": "the source belongs to one team for one child; onboard another for another",
    "status": "cancel with DELETE, on the event or on one occurrence",
    "uid": "the external_id in the path is the event's identity",
    "external_id": "the external_id is taken from the path",
}

#: Safe in a URL path segment, an iCalendar UID and a Radicale filename alike.
EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_TEXT = 500
MAX_NOTES = 4000
MAX_SPAN = timedelta(days=7)


class Invalid(ValueError):
    """One or more fields are wrong. Carries every problem, not the first."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{e['field']}: {e['detail']}" for e in errors))


def _err(field_: str, code: str, detail: str) -> dict:
    return {"field": field_, "code": code, "detail": detail}


@dataclass(frozen=True)
class Instance:
    """One occurrence, ready to become an `Event`."""

    uid: str
    key: str | None            # occurrence date for a series, None for a single event
    start: datetime            # aware
    end: datetime              # aware
    all_day: bool
    tz: str
    fields: dict = field(default_factory=dict)


# --- keys --------------------------------------------------------------------


def new_key() -> str:
    """A fresh API key. Shown once; only its hash is kept."""
    return "csk_" + _secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    """SHA-256, not bcrypt: the key is 256 random bits, not a password a person
    chose, so there is no dictionary to slow down and a fast hash is the
    standard treatment — the same one GitHub gives its tokens."""
    return hashlib.sha256(key.encode()).hexdigest()


def key_matches(presented: str, stored_hash: str | None) -> bool:
    if not stored_hash or not presented:
        return False
    return hmac.compare_digest(hash_key(presented), stored_hash)


# --- reading times -----------------------------------------------------------


def _local(value, zone: ZoneInfo, field_: str, errors: list[dict]):
    """A date, or a naive local datetime. An offset is honoured, then dropped:
    a series repeats in wall-clock time, so that is what is stored."""
    if not isinstance(value, str) or not value.strip():
        errors.append(_err(field_, "required", "give an ISO date or local datetime"))
        return None
    text = value.strip()
    if _DATE.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            errors.append(_err(field_, "bad_date", f"{text!r} is not a real date"))
            return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        errors.append(_err(field_, "bad_datetime",
                           f"{text!r} is not ISO 8601, e.g. 2026-10-20T17:45:00"))
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(zone).replace(tzinfo=None)
    return moment.replace(microsecond=0)


def _text(body: dict, name: str, errors: list[dict], *, limit: int = MAX_TEXT):
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(_err(name, "not_text", "must be a string"))
        return None
    value = value.strip()
    if len(value) > limit:
        errors.append(_err(name, "too_long", f"at most {limit} characters"))
        return None
    return value or None


def _unknown(body: dict, allowed: set[str], errors: list[dict]) -> None:
    for name in sorted(set(body) - allowed):
        reason = _REFUSED.get(name)
        errors.append(_err(
            name, "not_accepted",
            reason or f"not a field calsync reads; accepted: {', '.join(sorted(allowed))}",
        ))


# --- validating ----------------------------------------------------------------


def validate_event(body, *, default_tz: str) -> dict:
    """The stored form of an event body, or :class:`Invalid` listing every problem."""
    if not isinstance(body, dict):
        raise Invalid([_err("", "not_an_object", "the body must be a JSON object")])
    errors: list[dict] = []
    _unknown(body, FIELDS, errors)

    kind = body.get("type")
    if kind not in TYPES:
        errors.append(_err("type", "bad_enum", f"must be one of {', '.join(TYPES)}"))

    tz = body.get("tz")
    if tz is not None and (not isinstance(tz, str) or not zones.loadable(tz)):
        errors.append(_err("tz", "bad_zone", f"{tz!r} is not an IANA zone, e.g. America/New_York"))
        tz = None
    zone = ZoneInfo(tz or default_tz)

    start = _local(body.get("start"), zone, "start", errors)
    end = None
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    if all_day:
        if body.get("end") is not None:
            end = _local(body.get("end"), zone, "end", errors)
            if end is not None and (isinstance(end, datetime) or end < start):
                errors.append(_err("end", "bad_end",
                                   "for an all-day event, end is the last day (a date), "
                                   "on or after start"))
    elif start is not None:
        end = _local(body.get("end"), zone, "end", errors)
        if end is not None:
            if not isinstance(end, datetime):
                errors.append(_err("end", "bad_end", "a timed event ends at a datetime"))
            elif end <= start:
                errors.append(_err("end", "end_before_start", "end must be after start"))
            elif end - start > MAX_SPAN:
                errors.append(_err("end", "too_long",
                                   "longer than 7 days; send an all-day event instead"))

    home = body.get("home")
    if home is not None and not isinstance(home, bool):
        errors.append(_err("home", "not_boolean", "true, false, or omit it when unknown"))
    opponent = _text(body, "opponent", errors)
    if kind == "practice" and (opponent or home is not None):
        errors.append(_err("opponent" if opponent else "home", "game_only",
                           "only a game has an opponent or a home/away side"))

    rrule = body.get("rrule")
    exdates: list[str] = []
    if rrule is not None:
        if all_day:
            errors.append(_err("rrule", "unsupported", "all-day events cannot repeat"))
        elif isinstance(start, datetime):
            try:
                rule = recurrence.parse(rrule)
                recurrence.expand(rule, start, zone)
            except recurrence.RuleError as exc:
                errors.append(_err("rrule", "bad_rrule", str(exc)))
    raw_ex = body.get("exdates") or []
    if raw_ex and rrule is None:
        errors.append(_err("exdates", "no_series", "exdates need an rrule"))
    elif not isinstance(raw_ex, list):
        errors.append(_err("exdates", "not_a_list", "a list of dates, e.g. [\"2026-11-24\"]"))
    else:
        for item in raw_ex:
            # A date, or a datetime whose date is used: senders following
            # RFC 5545 send the occurrence's start, and the date is the name.
            text = item.strip()[:10] if isinstance(item, str) else ""
            if not _DATE.match(text):
                errors.append(_err("exdates", "bad_date", f"{item!r} is not a date"))
            else:
                exdates.append(text)

    part_of = body.get("part_of")
    if part_of is not None and (not isinstance(part_of, str) or not EXTERNAL_ID.match(part_of)):
        errors.append(_err("part_of", "bad_id", "the external_id of another event on "
                                                 "this source"))
        part_of = None

    stored = {
        "type": kind,
        "label": _text(body, "label", errors),
        "location": _text(body, "location", errors),
        "opponent": opponent,
        "home": home,
        "notes": _text(body, "notes", errors, limit=MAX_NOTES),
        "url": _text(body, "url", errors),
        "tz": tz,
        "start": start.isoformat() if start is not None else None,
        "end": end.isoformat() if end is not None else None,
        "rrule": rrule.strip() if isinstance(rrule, str) else None,
        "exdates": sorted(set(exdates)),
    }
    # Only when given, so an event stored before part_of existed keeps its
    # ETag and an identical resend stays a no-op.
    if part_of:
        stored["part_of"] = part_of
    if errors:
        raise Invalid(errors)
    return stored


def validate_override(body, *, event: dict, key: str, default_tz: str) -> dict:
    """One occurrence's changes. Times are full local datetimes, like the event's."""
    if not isinstance(body, dict):
        raise Invalid([_err("", "not_an_object", "the body must be a JSON object")])
    errors: list[dict] = []
    _unknown(body, OVERRIDE_FIELDS, errors)
    zone = ZoneInfo(event.get("tz") or default_tz)
    out: dict = {}
    for name in ("location", "label"):
        if name in body:
            out[name] = _text(body, name, errors)
    if "notes" in body:
        out["notes"] = _text(body, "notes", errors, limit=MAX_NOTES)

    base_start = datetime.fromisoformat(event["start"])
    base_end = datetime.fromisoformat(event["end"])
    occurrence = datetime.combine(date.fromisoformat(key), base_start.time())
    start = occurrence
    end = None
    if "start" in body:
        start = _local(body["start"], zone, "start", errors)
        if start is not None and not isinstance(start, datetime):
            errors.append(_err("start", "bad_datetime", "an occurrence starts at a datetime"))
            start = None
    if "end" in body:
        end = _local(body["end"], zone, "end", errors)
        if end is not None and not isinstance(end, datetime):
            errors.append(_err("end", "bad_datetime", "an occurrence ends at a datetime"))
            end = None
    elif start is not None:
        end = start + (base_end - base_start)
    if isinstance(start, datetime) and isinstance(end, datetime):
        if end <= start:
            errors.append(_err("end", "end_before_start", "end must be after start"))
        if start != occurrence or "start" in body:
            out["start"] = start.isoformat()
        if "end" in body or "start" in body:
            out["end"] = end.isoformat()
    if errors:
        raise Invalid(errors)
    return out


# --- series ----------------------------------------------------------------------


def _uid(source_id: str, external_id: str, key: str | None) -> str:
    """Deterministic, so a repeated PUT can never mint a second event.

    An occurrence is named by its date, not its time: moving a practice from
    17:45 to 18:00 is an update to the same event, and only a change of *day*
    makes a different one.
    """
    stem = f"{source_id}.{external_id}"
    if key:
        stem += "." + key.replace("-", "")
    return f"{stem}@inbound.calsync"


def generated_keys(event: dict, *, default_tz: str, honour_exdates: bool = True) -> list[str]:
    """Occurrence dates a series produces. Empty for a single event."""
    if not event.get("rrule"):
        return []
    zone = ZoneInfo(event.get("tz") or default_tz)
    rule = recurrence.parse(event["rrule"])
    starts = recurrence.expand(rule, datetime.fromisoformat(event["start"]), zone)
    skip = set(event.get("exdates") or ()) if honour_exdates else set()
    return [k for k in map(recurrence.occurrence_key, starts) if k not in skip]


def instances(source_id: str, external_id: str, state: dict, *,
              default_tz: str) -> list[Instance]:
    """Every occurrence this item generates now, overrides applied.

    Ignores whether the item is cancelled: a cancelled item still names the
    events it would have been, and the adapter emits those as cancelled.
    """
    event = state["event"]
    tz = event.get("tz") or default_tz
    zone = ZoneInfo(tz)
    base = {k: event.get(k) for k in
            ("type", "label", "location", "opponent", "home", "notes", "url")}

    if event["start"] and _DATE.match(event["start"]):
        first = date.fromisoformat(event["start"])
        last = date.fromisoformat(event["end"]) if event.get("end") else first
        start = datetime(first.year, first.month, first.day, tzinfo=zone)
        # Exclusive, as RFC 5545 and `teamreach` both have it.
        end = datetime(last.year, last.month, last.day, tzinfo=zone) + timedelta(days=1)
        return [Instance(_uid(source_id, external_id, None), None, start, end,
                         True, tz, dict(base))]

    start = datetime.fromisoformat(event["start"])
    end = datetime.fromisoformat(event["end"])
    if not event.get("rrule"):
        return [Instance(_uid(source_id, external_id, None), None,
                         start.replace(tzinfo=zone), end.replace(tzinfo=zone),
                         False, tz, dict(base))]

    span = end - start
    overrides = state.get("overrides") or {}
    out = []
    for key in generated_keys(event, default_tz=default_tz):
        local = datetime.combine(date.fromisoformat(key), start.time())
        change = overrides.get(key, {})
        o_start = datetime.fromisoformat(change["start"]) if "start" in change else local
        o_end = (datetime.fromisoformat(change["end"]) if "end" in change
                 else o_start + span)
        fields_ = dict(base)
        for name in ("location", "notes", "label"):
            if name in change:
                fields_[name] = change[name]
        out.append(Instance(_uid(source_id, external_id, key), key,
                            o_start.replace(tzinfo=zone), o_end.replace(tzinfo=zone),
                            False, tz, fields_))
    return out


def retire(source_id: str, external_id: str, old: dict | None, new: dict, *,
           default_tz: str) -> list[dict]:
    """``new``'s retired list, given what ``old`` used to generate.

    An occurrence ``old`` produced and ``new`` does not is remembered so the
    adapter can say it is cancelled. One that comes back is forgotten again, so
    reinstating an exdate creates the event afresh like any other.
    """
    now_uids = {i.uid for i in instances(source_id, external_id, new, default_tz=default_tz)}
    kept = [r for r in (old or {}).get("retired", []) if r["uid"] not in now_uids]
    seen = {r["uid"] for r in kept}
    if old is not None:
        for gone in instances(source_id, external_id, old, default_tz=default_tz):
            if gone.uid not in now_uids and gone.uid not in seen:
                kept.append({
                    "uid": gone.uid, "start": gone.start.isoformat(),
                    "end": gone.end.isoformat(), "all_day": gone.all_day, "tz": gone.tz,
                    "type": gone.fields.get("type"),
                })
    return kept


def put_event(source_id: str, external_id: str, old: dict | None, event: dict, *,
              default_tz: str) -> tuple[dict, list[str]]:
    """Replace an item's event. Returns the new state and any orphaned overrides.

    An override survives a change to the series only if its date is still an
    occurrence. The rest are dropped and *named* in the response — a moved
    practice silently snapping back to the series time is the failure that
    makes a person stop trusting the calendar.
    """
    overrides = dict((old or {}).get("overrides") or {})
    keys = set(generated_keys(event, default_tz=default_tz))
    orphaned = sorted(k for k in overrides if k not in keys)
    for k in orphaned:
        del overrides[k]
    new = {"event": event, "overrides": overrides}
    new["retired"] = retire(source_id, external_id, old, new, default_tz=default_tz)
    return new, orphaned


class NotAnOccurrence(ValueError):
    """The date is not one the series produces."""


def _occurrence(state: dict, key: str, *, default_tz: str) -> None:
    if not state["event"].get("rrule"):
        raise NotAnOccurrence("this event does not repeat, so it has no occurrences; "
                              "PUT the event itself instead")
    if not _DATE.match(key):
        raise NotAnOccurrence(f"{key!r} is not a date (YYYY-MM-DD)")
    if key not in generated_keys(state["event"], default_tz=default_tz,
                                 honour_exdates=False):
        raise NotAnOccurrence(f"the series has no occurrence on {key}")


def put_override(source_id: str, external_id: str, state: dict, key: str,
                 override: dict, *, default_tz: str) -> dict:
    """Change one occurrence. Putting back an exdated one reinstates it."""
    _occurrence(state, key, default_tz=default_tz)
    event = dict(state["event"])
    event["exdates"] = [d for d in event.get("exdates") or [] if d != key]
    overrides = dict(state.get("overrides") or {})
    if override:
        overrides[key] = override
    else:
        overrides.pop(key, None)
    new = {"event": event, "overrides": overrides}
    new["retired"] = retire(source_id, external_id, state, new, default_tz=default_tz)
    return new


def cancel_occurrence(source_id: str, external_id: str, state: dict, key: str, *,
                      default_tz: str) -> dict:
    """Take one occurrence off. Idempotent: cancelling it twice changes nothing."""
    _occurrence(state, key, default_tz=default_tz)
    event = dict(state["event"])
    event["exdates"] = sorted(set(event.get("exdates") or []) | {key})
    overrides = dict(state.get("overrides") or {})
    overrides.pop(key, None)
    new = {"event": event, "overrides": overrides}
    new["retired"] = retire(source_id, external_id, state, new, default_tz=default_tz)
    return new


def etag(state: dict, cancelled: bool) -> str:
    """Over what the sender controls, so an identical PUT keeps its ETag."""
    body = json.dumps(
        {"event": state["event"], "overrides": state.get("overrides") or {},
         "cancelled": bool(cancelled)},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()[:24]


def content_hash(instance: Instance) -> str:
    """calsync's own hash over content, as every adapter computes one."""
    body = json.dumps(
        {"start": instance.start.isoformat(), "end": instance.end.isoformat(),
         "all_day": instance.all_day, "tz": instance.tz, **instance.fields},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def overlaps(source_id: str, external_id: str, state: dict, others, *,
             default_tz: str) -> list[dict]:
    """Dates where this new event collides with one already live.

    The failure this exists for is a sender with no memory: a fresh
    conversation sends Tuesday practice again under a new id, and every
    practice is on the calendar twice. Matching that needs no fuzzy title
    comparison — two events of the same type, overlapping in time on the same
    day, on one team's calendar are either a duplicate or something the sender
    can say on purpose (``allow_overlap``).

    ``others`` is ``(external_id, state)`` for every *live* item but this one.
    """
    mine = instances(source_id, external_id, state, default_tz=default_tz)
    kind = state["event"].get("type")
    parent = state["event"].get("part_of")
    found = []
    for other_id, other in others:
        if other["event"].get("type") != kind:
            continue
        # A session overlaps the meet it belongs to by design. Two sessions of
        # one meet at the same time still count: that is a real mistake.
        if other_id == parent or other["event"].get("part_of") == external_id:
            continue
        for theirs in instances(source_id, other_id, other, default_tz=default_tz):
            for ours in mine:
                if ours.start < theirs.end and theirs.start < ours.end:
                    found.append({
                        "external_id": other_id,
                        "date": theirs.start.astimezone(ZoneInfo(theirs.tz)).date().isoformat(),
                    })
                    break
    return found


# --- part_of -------------------------------------------------------------------------


def parent_problems(external_id: str, event: dict, items: dict) -> list[dict]:
    """Why ``event``'s ``part_of`` cannot be accepted, or nothing.

    ``items`` is ``{external_id: state}`` for every item on the source, this one
    included if it exists. One level only: a meet has sessions, and a session
    has no sessions of its own. That keeps "is this on the calendar?" a single
    lookup rather than a walk up a tree.
    """
    parent = event.get("part_of")
    if not parent:
        return []
    if parent == external_id:
        return [_err("part_of", "self", "an event cannot be part of itself")]
    if parent not in items:
        return [_err("part_of", "no_such_event",
                     f"nothing has been sent with id {parent!r} on this source; "
                     "send the meet first")]
    if items[parent]["event"].get("part_of"):
        return [_err("part_of", "too_deep",
                     f"{parent!r} is itself part of another event; point at that one")]
    if any(s["event"].get("part_of") == external_id
           for other_id, s in items.items() if other_id != external_id):
        return [_err("part_of", "has_parts",
                     "other events are part of this one, so it cannot be part of "
                     "something else")]
    return []


def live_parts(items) -> dict[str, list[str]]:
    """``{meet: [its live parts]}``, for every event that has any.

    ``items`` is ``(external_id, state, cancelled)``.
    """
    parts: dict[str, list[str]] = {}
    for external_id, state, cancelled in items:
        parent = state["event"].get("part_of")
        if parent and not cancelled:
            parts.setdefault(parent, []).append(external_id)
    return {parent: sorted(ids) for parent, ids in parts.items()}


def superseded(items) -> set[str]:
    """Events taken off the calendar because their parts have arrived.

    A meet announced as an all-day placeholder is replaced by its sessions
    while at least one *live* part points at it, and comes back if every part
    is withdrawn — the meet is still on even when the session list is pulled.

    **The whole meet, not the days that have sessions.** The placeholder is
    one multi-day event, and a sender sends only the sessions a swimmer is
    in, so a day with no part is a day they are not swimming. A sender that
    learns the schedule a day at a time should wait for the whole of it.
    """
    return set(live_parts(items))
