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
    #: Away games need travel time; practices are usually local.
    alarm_game_min: int = 90
    alarm_practice_min: int = 30
    #: Minutes before kick-off the team expects you at the ground, or 0 for a
    #: team that does not ask. Per team rather than instance-wide because it is
    #: a coach's rule, not a household's: one club wants 45 minutes and the
    #: rec team down the road wants none. See `warmup.py`.
    warmup_minutes: int = 0

    def alarm_minutes(self, *, is_game: bool) -> int:
        return self.alarm_game_min if is_game else self.alarm_practice_min

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def known_tokens(self) -> tuple[str, ...]:
        """Strings that identify *us*, and so carry no information in a title.

        Longest first, so "Vanguard Academy" is consumed before "Vanguard" can chip
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
    #: Which field/court/gym *within* the venue — "#2", "Field 3", "Gym".
    #: Deliberately not part of venue identity: "Kingsmere #2" and
    #: "Kingsmere #4" are one place with one pin, and keeping the designator
    #: out of the name is what lets a single alias row cover them all.
    field: str | None = None

    @property
    def resolved(self) -> bool:
        return self.lat is not None and self.lon is not None


#: Per-event questions that only judgment can answer, and that change *where
#: the event goes* rather than merely how it reads. Kept distinct from
#: `PollResult.diagnostics`, which aggregates the same findings per source as a
#: bag of unrecognised strings: that is the right shape for "what does this feed
#: still need", and useless for "may this particular event go on the calendar".
#:
#: Only placement-affecting questions belong here. An unresolved *venue* does
#: not: it costs a map pin, the event still carries its location as text, and
#: holding a fixture back over it would make a game the family needs to know
#: about invisible. Measured on the fixtures, venues go unresolved constantly
#: and have never once changed a collection.
UNKNOWN_TYPE = "unknown_type"          # is this label a game or a practice?
UNKNOWN_CATEGORY = "unknown_category"  # same question, Player360's vocabulary
UNIDENTIFIED = "unidentified"          # which side of this fixture is us?

#: The `PollResult.diagnostics` kinds matching the per-event blockers above.
#: `unresolved_venues` is deliberately absent: it is a real gap and it holds
#: nothing back, so anything deciding "is an event waiting on somebody" must not
#: count it.
BLOCKING_DIAGNOSTICS = ("unknown_types", "unknown_categories", "unidentified")


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
    #: The UID of the game this warm-up sits in front of, or None for every
    #: event a feed actually published. Set only by `warmup.expand`. It is what
    #: makes a synthetic event legible downstream — which title template to use,
    #: which alarm to take, and whether the diff's guard should count it.
    warmup_for: str | None = None

    #: A date with no time — a tournament day a coach entered before the
    #: schedule existed. `starts_at` is still an instant, local midnight in the
    #: activity's timezone, so windowing, ordering and the diff need no special
    #: case. Only rendering does: writing 00:00 would put "Semifinal Games" at
    #: midnight and fire its alarm the previous evening.
    all_day: bool = False
    #: The feed's own LAST-MODIFIED, carried but never trusted for change
    #: detection — `content_hash` remains the authority, and this is excluded
    #: from it. Player360 bumps this 2-5s after an event *ends*, so it is noise
    #: for all but one purpose: a move of it to *before* the event, with our
    #: content unchanged, is an upstream edit whose substance the feed does not
    #: publish. That is how a cancellation reaches us and the only trace of it.
    upstream_modified_at: datetime | None = None

    #: Questions blocking this event's placement (UNKNOWN_TYPE and friends).
    #:
    #: The adapters have always known this per event and thrown it away: an
    #: unclassifiable event had `is_game` coerced to False and joined the
    #: practices, so nothing downstream could tell a known practice from one we
    #: could not place. On one real feed that put 12 of 20 events — most of a
    #: season — in the wrong calendar, and correcting it later is a *move*,
    #: which is the delete-then-create this project treats as the dangerous
    #: operation.
    #:
    #: `is_game` still carries its safe default, so nothing downstream has to
    #: handle a third state and the behaviour degrades to what it was if
    #: enrichment is switched off.
    unresolved: tuple[str, ...] = ()

    @property
    def needs_enrichment(self) -> bool:
        """Should this be held off the real calendar until somebody answers?"""
        return bool(self.unresolved)

    @property
    def is_warmup(self) -> bool:
        return self.warmup_for is not None

    @property
    def alarms_as_game(self) -> bool:
        """Take the game's alarm rather than the practice one?

        A warm-up is not a game, but it is the thing you leave the house for —
        so the reminder that matters is the game's travel-time one, timed off
        the warm-up. The game keeps its own alarm as well: the two fire far
        enough apart to mean different things, where a practice alarm on a
        warm-up would land minutes from the game's and read as a duplicate.
        """
        return self.is_game or self.is_warmup


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

    #: Parse gaps the adapter could not resolve, keyed by kind — "unknown_types",
    #: "unknown_categories", "unidentified". Open-ended because each feed fails in
    #: its own way, and an adapter must be able to report a new kind without a
    #: schema change.
    #:
    #: This is the promotion gate (docs/ONBOARDING.md §5): a staged source is
    #: ready for the real calendar when every list here is empty. Anything an
    #: adapter reports here is a question for a human or for Hermes.
    diagnostics: dict[str, list[str]] = field(default_factory=dict)

    def report(self, kind: str, values) -> None:
        values = sorted(v for v in values if v)
        if values:
            self.diagnostics[kind] = values

    @property
    def is_clean(self) -> bool:
        """Nothing the adapter could not account for."""
        return not any(self.diagnostics.values())

    # Named accessors for the kinds in use, so callers and tests read plainly.
    @property
    def unknown_types(self) -> list[str]:
        return self.diagnostics.get("unknown_types", [])

    @property
    def unknown_categories(self) -> list[str]:
        return self.diagnostics.get("unknown_categories", [])

    @property
    def unidentified(self) -> list[str]:
        return self.diagnostics.get("unidentified", [])
