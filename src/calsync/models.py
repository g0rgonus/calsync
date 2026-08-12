"""Core domain types.

Deliberately plain dataclasses: these cross the boundary between source
adapters, normalization, and the CalDAV writer, and they are what the golden
tests assert against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Child:
    id: str
    name: str
    initial: str
    birth_order: int
    nicknames: tuple[str, ...] = ()


@dataclass(frozen=True)
class Activity:
    id: str
    child_id: str
    name: str
    sport: str
    emoji: str
    tz: str
    official_name: str | None = None
    short_name: str | None = None
    league: str | None = None
    age_group: str | None = None
    home_venue: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def known_tokens(self) -> tuple[str, ...]:
        """Strings that identify *us*, and so carry no information in a title.

        Longest first, so "Rush Academy" is consumed before "Rush" can chip
        a fragment off it and leave "Academy" behind.
        """
        raw = [self.name, self.official_name, self.short_name,
               self.league, self.age_group, *self.aliases]
        seen: dict[str, None] = {}
        for t in raw:
            if t:
                seen.setdefault(t, None)
        return tuple(sorted(seen, key=len, reverse=True))


@dataclass(frozen=True)
class Venue:
    raw: str
    name: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    pin_confirmed: bool = False

    @property
    def resolved(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class Event:
    """A normalized event, before it becomes a VEVENT.

    `summary` is deliberately absent — the display title is rendered from these
    fields at write time (see normalize/title.py), never stored.
    """

    uid: str
    activity_id: str
    starts_at: datetime
    ends_at: datetime
    is_game: bool
    tz: str
    venue: Venue | None = None
    opponent: str | None = None
    home: bool | None = None
    detail: str | None = None
    body: str | None = None
    url: str | None = None
    source_id: str | None = None
    source_category: str | None = None
    content_hash: str | None = None
    kit: str | None = None
    arrive_at: datetime | None = None

    def __post_init__(self) -> None:
        # Absolute instants only. A naive datetime here means an adapter lost
        # the offset, which silently shifts every downstream render.
        for name in ("starts_at", "ends_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
                raise ValueError(f"{name} must be timezone-aware, got {value!r}")
        if self.ends_at < self.starts_at:
            raise ValueError(f"ends_at {self.ends_at} precedes starts_at {self.starts_at}")

    @property
    def local_start(self) -> datetime:
        """Start in the *venue's* timezone, which is what people plan around."""
        return self.starts_at.astimezone(ZoneInfo(self.tz))

    @property
    def collection(self) -> str:
        return "games" if self.is_game else "practices"


@dataclass
class PollResult:
    source_id: str
    events: list[Event] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_sha256: str | None = None
