"""One occurrence, many teams: when kids on different teams go to the same meet or practice.

Pure: no database, no clock. `sync.py` stores each source's candidates
(`shared_candidates`), groups them by :func:`key`, and has the lowest source id
write the event for everybody; the rest drop their copy the way a withheld
event is dropped. The rule is in `docs/MATCHING.md`, "One meet or practice, two teams".
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import Event


def _name(text: str | None) -> str:
    """Casefolded, punctuation-free, single-spaced; None and "" are the same."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def key(event: Event, sport: str, collection: str) -> str | None:
    """The blocking key for an event, or None when it can never be merged.

    Venue is left out on purpose: it is checked per group (:func:`venues_conflict`)
    because an unresolved venue has no opinion and must not split a group.
    ``collection`` is where the copy would be written: the merged event lives
    on the carrier's calendar only, so copies bound for different ones never merge.
    """
    if event.is_warmup or event.cancelled:
        return None
    zone = ZoneInfo(event.tz)
    start, end = event.starts_at.astimezone(zone), event.ends_at.astimezone(zone)
    # Timed never matches all-day, and a timed pair must agree to the instant.
    when = ("day",) if event.all_day else (
        "timed", event.starts_at.astimezone(timezone.utc).isoformat(),
        event.ends_at.astimezone(timezone.utc).isoformat())
    return json.dumps([sport, collection, start.date().isoformat(), end.date().isoformat(), *when,
                       _name(event.detail), _name(event.opponent)])


def carrier(source_ids) -> str:
    """The source that writes the event: the lowest id, so it never depends on order."""
    return min(source_ids)


def _distinct(values) -> set[str]:
    return {v.casefold() for v in values if v}


def venues_conflict(rows) -> bool:
    """Do the members disagree about where it is? Then nobody merges.

    Two meets with the same name at different pools are two meets; a field
    that differs is the same doubt. An unresolved venue is None, no opinion.
    """
    return (len(_distinct(r["venue"] for r in rows)) > 1
            or len(_distinct(r["field"] for r in rows)) > 1)


def usable(rows) -> bool:
    """Is this group one occurrence? Two sources or more, one row each, no venue clash.

    A source with two rows under one key is ambiguous (which one is the
    meet?), so that group writes everyone's own.
    """
    ids = [r["source_id"] for r in rows]
    return len(set(ids)) > 1 and len(set(ids)) == len(ids) and not venues_conflict(rows)


@dataclass(frozen=True)
class Merged:
    kids: tuple[str, ...]          # child ids, sorted
    body: str
    url: str | None
    kit: str | None
    arrive_at: datetime | None

    @property
    def layer(self) -> str:
        """What was laid over the carrier's own content, as canonical JSON.

        Stored as the receipt (`event_state.merged`): equality says whether the
        phone is current, and readers lay it over the stored content.
        """
        return json.dumps({"body": self.body, "kit": self.kit,
                           "arrive_at": self.arrive_at.isoformat() if self.arrive_at else None},
                          sort_keys=True)


def merge(members) -> Merged:
    """What the carrier renders: every member's kids and notes, said once."""
    ordered = sorted(members, key=lambda r: (r["kid_order"], r["source_id"]))
    lead = min(members, key=lambda r: r["source_id"])
    kids = sorted({k for r in members for k in r["kids"].split(",") if k})

    lines: list[str] = []
    for r in ordered:
        for line in (r["body"] or "").splitlines():
            line = line.strip()
            if line and line not in lines:
                lines.append(line)
    for r in ordered:
        url = r["url"]
        if url and url != lead["url"] and url not in lines:
            lines.append(url)

    kits: list[str] = []
    for r in ordered:
        if r["kit"] and r["kit"] not in kits:
            kits.append(r["kit"])
    arrivals = [datetime.fromisoformat(r["arrive_at"]) for r in members if r["arrive_at"]]

    body, kit = "\n".join(lines), " / ".join(kits) or None
    arrive = min(arrivals) if arrivals else None
    return Merged(tuple(kids), body, lead["url"], kit, arrive)
