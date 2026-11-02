"""The admin API's request checks: what a new team must say, and nothing else.

Every problem is reported at once, like `events.validate_event`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

from .. import repo, withheld, zones
from ..normalize import venue as venue_norm
from .events import Invalid, _err

FIELDS = frozenset({"name", "children", "sport", "tz", "season_end",
                    "alarm_game_min", "alarm_practice_min"})
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
STATUS_FIELDS = frozenset({"uid", "status", "note", "child"})
MAX_NOTE = 200
VENUE_FIELDS = frozenset({"name", "short_name", "address", "aliases"})
MAX_VENUE_TEXT = 200
MAX_NAME = 80
MAX_ALARM_MIN = 24 * 60


def validate_team(body, conn) -> dict:
    """A create body, checked against the household's kids and sports.

    A null is treated as a missing field, so it takes the default.
    """
    if not isinstance(body, dict):
        raise Invalid([_err("body", "not_an_object", "send a JSON object")])
    body = {k: v for k, v in body.items() if v is not None}
    errors: list[dict] = []
    for name in sorted(set(body) - FIELDS):
        errors.append(_err(name, "unknown_field", "not a field of a team"))

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(_err("name", "required", "the team's name"))
    elif len(name.strip()) > MAX_NAME:
        errors.append(_err("name", "too_long", f"at most {MAX_NAME} characters"))

    kids = {c.id for c in repo.list_children(conn)}
    children = body.get("children")
    if not isinstance(children, list) or not children:
        errors.append(_err("children", "required", "a list with the kid's id"))
    elif not all(isinstance(c, str) and c in kids for c in children):
        errors.append({**_err("children", "unknown_child",
                              "not a kid in calsync; add new kids in the console"),
                       "candidates": sorted(kids)})
    elif len(set(children)) != len(children):
        errors.append(_err("children", "repeated", "each kid once"))

    sports = {r["id"] for r in repo.list_sports(conn)}
    sport = body.get("sport")
    if not isinstance(sport, str) or sport not in sports:
        errors.append({**_err("sport", "unknown_sport", "not a sport in calsync"),
                       "candidates": sorted(sports)})

    tz = body.get("tz")
    if tz is not None and not (isinstance(tz, str) and zones.loadable(tz)):
        errors.append(_err("tz", "bad_zone", "an IANA zone, e.g. America/New_York"))

    season_end = body.get("season_end")
    if season_end is not None:
        try:
            if not ISO_DATE.match(season_end):
                raise ValueError
            date.fromisoformat(season_end)
        except (TypeError, ValueError):
            errors.append(_err("season_end", "bad_date", "a date, YYYY-MM-DD"))

    for field_ in ("alarm_game_min", "alarm_practice_min"):
        value = body.get(field_)
        if value is not None and not (isinstance(value, int) and not isinstance(value, bool)
                                      and 0 <= value <= MAX_ALARM_MIN):
            errors.append(_err(field_, "bad_minutes", f"whole minutes, 0 to {MAX_ALARM_MIN}"))

    if errors:
        raise Invalid(errors)
    return body


def validate_status(body) -> dict:
    """An event status change: a uid, a reason or null to clear, and a note."""
    if not isinstance(body, dict):
        raise Invalid([_err("body", "not_an_object", "send a JSON object")])
    errors = [_err(n, "unknown_field", "not a field of a status change")
              for n in sorted(set(body) - STATUS_FIELDS)]
    if not isinstance(body.get("uid"), str) or not body["uid"]:
        errors.append(_err("uid", "required", "the event's uid, from the calendar read"))
    if "status" not in body:
        errors.append(_err("status", "required", "not_attending, cancelled, or null "
                                                 "to put the event back"))
    elif body["status"] is not None and (not isinstance(body["status"], str)
                                         or body["status"] not in withheld.REASONS):
        errors.append({**_err("status", "unknown_status", "not a status"),
                       "candidates": sorted(withheld.REASONS)})
    child = body.get("child")
    if child is not None and not (isinstance(child, str) and child):
        errors.append(_err("child", "bad_child", "a kid's id"))
    elif child is not None and body.get("status") == "cancelled":
        errors.append(_err("child", "not_for_cancelled",
                           "an event is cancelled for everyone; leave child out"))
    note = body.get("note")
    if note is not None and not (isinstance(note, str) and len(note.strip()) <= MAX_NOTE):
        errors.append(_err("note", "bad_note", f"text, at most {MAX_NOTE} characters"))
    if errors:
        raise Invalid(errors)
    return {**body, "note": (note or "").strip() or None}


def _venue_name(name, field_: str, errors: list[dict]) -> None:
    if not isinstance(name, str) or not name.strip():
        errors.append(_err(field_, "required", "a name"))
    elif len(name.strip()) > MAX_VENUE_TEXT:
        errors.append(_err(field_, "too_long", f"at most {MAX_VENUE_TEXT} characters"))
    else:
        base, designator = venue_norm.split_field(name.strip())
        if designator and base:
            errors.append({**_err(field_, "field_in_name",
                                  f"{designator} is a field at {base}; a venue is the "
                                  "place, and the field stays in the event's location"),
                           "suggestion": base})


def validate_venue(body) -> dict:
    """A new venue: a name, and optionally an address, short name and aliases."""
    if not isinstance(body, dict):
        raise Invalid([_err("body", "not_an_object", "send a JSON object")])
    body = {k: v for k, v in body.items() if v is not None}
    errors = [_err(n, "unknown_field", "not a field of a venue")
              for n in sorted(set(body) - VENUE_FIELDS)]
    _venue_name(body.get("name"), "name", errors)
    for field_ in ("short_name", "address"):
        value = body.get(field_)
        if value is not None and not (isinstance(value, str)
                                      and len(value.strip()) <= MAX_VENUE_TEXT):
            errors.append(_err(field_, "bad_text", f"text, at most {MAX_VENUE_TEXT} "
                                                   "characters"))
    aliases = body.get("aliases", [])
    if not isinstance(aliases, list) or not all(
            isinstance(a, str) and a.strip() and len(a.strip()) <= MAX_VENUE_TEXT
            for a in aliases):
        errors.append(_err("aliases", "bad_aliases", "a list of names the feeds use"))
    if errors:
        raise Invalid(errors)
    name = body["name"].strip()
    unique: dict[str, str] = {name.casefold(): name}
    for a in aliases:
        unique.setdefault(a.strip().casefold(), a.strip())
    return {"name": name,
            "short_name": (body.get("short_name") or "").strip() or None,
            "address": (body.get("address") or "").strip() or None,
            "aliases": sorted(v for k, v in unique.items() if k != name.casefold())}


def validate_alias(body) -> str:
    if not isinstance(body, dict):
        raise Invalid([_err("body", "not_an_object", "send a JSON object")])
    errors = [_err(n, "unknown_field", "only alias") for n in sorted(set(body) - {"alias"})]
    alias = body.get("alias")
    if not isinstance(alias, str) or not alias.strip() or len(alias.strip()) > MAX_VENUE_TEXT:
        errors.append(_err("alias", "required", "the place as a feed or person wrote it"))
    if errors:
        raise Invalid(errors)
    return alias.strip()


def validate_home(body) -> int | None:
    if not isinstance(body, dict) or "venue_id" not in body or set(body) - {"venue_id"}:
        raise Invalid([_err("venue_id", "required", "a venue id, or null for none")])
    value = body["venue_id"]
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                              or not 0 < value < 2 ** 63):
        raise Invalid([_err("venue_id", "bad_id", "a venue id, or null for none")])
    return value


def request_hash(body) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
