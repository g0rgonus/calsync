"""The part of RFC 5545's RRULE a team schedule actually uses, expanded here.

calsync writes one VEVENT per occurrence and never an RRULE master. The diff,
`event_content`, `/v1/placements`, the disappearance guard and the Mac mirror's
parser all work one event at a time, and a recurring master would need every
one of them to learn about overrides. So a series arriving over the inbound API
is expanded into instances on the way in, and from there on it is a list of
events like any feed.

Expansion happens in **local wall-clock time** and is only then anchored to the
zone. That is what keeps a 17:45 practice at 17:45 across the November change:
the instant moves by an hour, the time on the pool deck does not. Expanding in
UTC would do the opposite.

Deliberately a subset. ``FREQ=DAILY|WEEKLY`` with ``INTERVAL``, ``BYDAY``
(plain weekdays, no ordinals), ``WKST=MO`` and exactly one of ``UNTIL`` or
``COUNT``. Anything else is refused with a message, not approximated: a rule
read half-right puts practices on days nobody is at the pool, and an unbounded
rule has no season end — which is a decision for whoever sends it, never a
default here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

#: A season of daily practice is about 300. Past this a rule is a mistake.
MAX_OCCURRENCES = 400

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

_UNTIL_UTC = re.compile(r"^(\d{8})T(\d{6})Z$")
_UNTIL_LOCAL = re.compile(r"^(\d{8})T(\d{6})$")
_UNTIL_DATE = re.compile(r"^(\d{8})$")

_SUPPORTED = "FREQ=DAILY|WEEKLY with INTERVAL, BYDAY, WKST=MO and one of UNTIL or COUNT"


class RuleError(ValueError):
    """The rule is malformed, unsupported, or does not fit its start."""


@dataclass(frozen=True)
class Rule:
    freq: str                       # DAILY | WEEKLY
    interval: int = 1
    by_day: tuple[int, ...] = ()    # 0 = Monday
    until: tuple[str, object] | None = None   # ("utc"|"local"|"date", value)
    count: int | None = None


def parse(text: str) -> Rule:
    if not isinstance(text, str) or not text.strip():
        raise RuleError("rrule is empty")
    body = text.strip()
    if body.upper().startswith("RRULE:"):
        body = body[len("RRULE:"):]

    parts: dict[str, str] = {}
    for chunk in body.split(";"):
        if not chunk:
            continue
        name, sep, value = chunk.partition("=")
        if not sep or not value:
            raise RuleError(f"{chunk!r} is not NAME=VALUE")
        name = name.strip().upper()
        if name in parts:
            raise RuleError(f"{name} appears twice")
        parts[name] = value.strip().upper()

    unknown = set(parts) - {"FREQ", "INTERVAL", "BYDAY", "UNTIL", "COUNT", "WKST"}
    if unknown:
        raise RuleError(f"unsupported rule part(s) {', '.join(sorted(unknown))}; "
                        f"calsync accepts {_SUPPORTED}")

    freq = parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY"):
        raise RuleError(f"FREQ={freq or '(missing)'} is not supported; calsync accepts {_SUPPORTED}")
    if parts.get("WKST", "MO") != "MO":
        raise RuleError("only WKST=MO is supported")

    interval = 1
    if "INTERVAL" in parts:
        if not parts["INTERVAL"].isdigit() or int(parts["INTERVAL"]) < 1:
            raise RuleError("INTERVAL must be a positive whole number")
        interval = int(parts["INTERVAL"])

    by_day: tuple[int, ...] = ()
    if "BYDAY" in parts:
        if freq != "WEEKLY":
            raise RuleError("BYDAY is only supported with FREQ=WEEKLY")
        days = []
        for day in parts["BYDAY"].split(","):
            if day not in WEEKDAYS:
                raise RuleError(f"BYDAY value {day!r} is not one of {','.join(WEEKDAYS)} "
                                "(ordinals like 2TU are not supported)")
            days.append(WEEKDAYS.index(day))
        by_day = tuple(sorted(set(days)))

    if ("UNTIL" in parts) == ("COUNT" in parts):
        raise RuleError("give exactly one of UNTIL or COUNT — a series needs an end, "
                        "and RFC 5545 forbids both")

    count = None
    until = None
    if "COUNT" in parts:
        if not parts["COUNT"].isdigit() or int(parts["COUNT"]) < 1:
            raise RuleError("COUNT must be a positive whole number")
        count = int(parts["COUNT"])
    else:
        until = _until(parts["UNTIL"])

    return Rule(freq=freq, interval=interval, by_day=by_day, until=until, count=count)


def _until(value: str) -> tuple[str, object]:
    try:
        if m := _UNTIL_UTC.match(value):
            return "utc", datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc)
        if m := _UNTIL_LOCAL.match(value):
            return "local", datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S")
        if m := _UNTIL_DATE.match(value):
            return "date", datetime.strptime(m[1], "%Y%m%d").date()
    except ValueError:
        pass
    raise RuleError(f"UNTIL={value} is not YYYYMMDD, YYYYMMDDTHHMMSS or YYYYMMDDTHHMMSSZ")


def _within(rule: Rule, local: datetime, zone: ZoneInfo) -> bool:
    kind, value = rule.until
    if kind == "utc":
        return local.replace(tzinfo=zone) <= value
    if kind == "local":
        return local <= value
    return local.date() <= value


def _candidates(rule: Rule, start: datetime):
    """Every slot the rule names, in order, from the start's own week or day."""
    if rule.freq == "DAILY":
        step = timedelta(days=rule.interval)
        current = start
        while True:
            yield current
            current += step
    days = rule.by_day or (start.weekday(),)
    week = start - timedelta(days=start.weekday())
    while True:
        for day in days:
            yield week + timedelta(days=day)
        week += timedelta(weeks=rule.interval)


def expand(rule: Rule, start: datetime, zone: ZoneInfo) -> list[datetime]:
    """Naive local starts of every occurrence, ``start`` first.

    ``start`` is naive local wall-clock time. It must itself be an occurrence:
    RFC 5545 leaves a DTSTART off its own rule undefined, and in practice it
    means the sender got either the date or the BYDAY wrong.
    """
    if start.tzinfo is not None:
        raise ValueError("expand takes naive local time")
    if rule.by_day and start.weekday() not in rule.by_day:
        raise RuleError(
            f"the start falls on a {WEEKDAYS[start.weekday()]} but BYDAY is "
            f"{','.join(WEEKDAYS[d] for d in rule.by_day)}"
        )

    out: list[datetime] = []
    for slot in _candidates(rule, start):
        if slot < start:
            continue
        if rule.count is not None and len(out) >= rule.count:
            break
        if rule.until is not None and not _within(rule, slot, zone):
            break
        out.append(slot)
        if len(out) > MAX_OCCURRENCES:
            raise RuleError(f"the rule produces more than {MAX_OCCURRENCES} occurrences")
    return out


def occurrence_key(local_start: datetime | date) -> str:
    """How one occurrence is named: its local date. Daily and weekly rules never
    put two on one day, and a date is what a person calls it ("the 27th")."""
    return local_start.strftime("%Y-%m-%d")
