"""TeamReach ICS adapter.

Findings in docs/sources/teamreach.md.

**There is no TeamReach format.** Coaches type these events by hand, and which
fields even exist varies per team. Three real feeds, three conventions:

    Inter HURRICANES   no LOCATION    "Game - Riverview #2"    type then VENUE
    Hawks              LOCATION       "Hawks vs Strikers"      US vs THEM
    Comets             LOCATION       "Game vs Jaguars"        type then OPPONENT

A parser built around any one of those silently mangles the other two — the
"Hawks vs Strikers" feed has no type word at all, so an adapter keyed on
"Game"/"Practice" files every fixture as a practice.

So this reads by *strategy* rather than by format: try each known shape, use
whichever fires, and when none does, say so through ``unknown_types`` /
``unidentified`` instead of guessing. Structure is treated as advisory;
meaning is never invented.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from icalendar import Calendar

from ..models import Activity, Event, PollResult, Venue
from ..normalize import venue as venue_norm
from . import register

#: Fields that constitute a real change. TeamReach publishes no SEQUENCE at all,
#: and DTSTAMP is written equal to LAST-MODIFIED on every event, so both are
#: modification timestamps rather than content and are excluded — same reasoning
#: as Player360, arrived at from the opposite direction.
HASH_FIELDS = ("DTSTART", "DTEND", "SUMMARY", "LOCATION", "DESCRIPTION")

#: Separator between event type and venue. The coach types "-", " - ", "  - "
#: and "- " interchangeably.
_SEPARATOR = re.compile(r"\s*-\s*")

#: Opponent separator. Deliberately excludes a bare "at": "Practice at Riverview"
#: names a venue, not an opponent, and treating it as a fixture would invent one.
_VERSUS = re.compile(r"\s+(?:vs\.?|v\.)\s+|\s+@\s+", re.IGNORECASE)

#: Leading words that name an event type rather than a team, so they can be
#: lifted off the left of a "vs" without being mistaken for an opponent.
_TYPE_PREFIX = re.compile(
    r"^\s*((?:first|last|final|make[\s-]?up|playoff|championship|semi[\s-]?final|"
    r"rescheduled|scrimmage|friendly|practice|training|game)\s*)+",
    re.IGNORECASE,
)

#: A trailing "6pm" / "6:30 pm" / "18:00" that the DTSTART already carries.
#: Requires a meridiem or a ``:`` — a bare trailing number is not a time, and
#: stripping one would turn "Riverview #2" into "Riverview #".
_TRAILING_TIME = re.compile(
    r"[\s.,]+(?:\d{1,2}[:.]\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))\s*$",
    re.IGNORECASE,
)

_WS = re.compile(r"\s+")

#: Any type containing this is a game — covers "Game", "Playoff Game",
#: "Make Up Game", "Rescheduled Playoff Game" and the observed "Playoff Game2"
#: without enumerating a vocabulary the coach can extend at will.
_GAME_WORD = re.compile(r"\bgame\b|\bgame\d+\b", re.IGNORECASE)
_PRACTICE_WORD = re.compile(r"\bpractice\b|\btraining\b", re.IGNORECASE)


class FeedError(RuntimeError):
    """The feed could not be trusted. Never treat this as 'everything cancelled'."""


def _text(component, key: str) -> str | None:
    value = component.get(key)
    if value is None:
        return None
    return str(value).strip() or None


def _dt(component, key: str) -> datetime | None:
    prop = component.get(key)
    if prop is None:
        return None
    value = prop.dt
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        # The feed publishes Z-suffixed UTC. A naive value means the parse lost
        # the offset, and guessing a zone shifts every render.
        raise FeedError(f"{key} has no timezone: {value!r}")
    return value.astimezone(timezone.utc)


def clean_venue(raw: str) -> str:
    """Tidy the venue half of a SUMMARY without renaming anything.

    Whitespace, a trailing period and a redundant trailing time are noise. A
    missing space before ``#`` is closed up because "Riverview#2" and
    "Riverview #2" are demonstrably the same field in the same feed.

    Anything beyond that is left alone — mapping variants onto one canonical
    venue is the ``venue_aliases`` table's job, not a regex's.
    """
    value = _TRAILING_TIME.sub("", raw)
    value = re.sub(r"(?<=\S)#", " #", value)
    return _WS.sub(" ", value).strip(" .,-")


def classify(event_type: str | None, *, has_opponent: bool = False) -> bool | None:
    """True for a game, False for a practice, None if undeterminable.

    An explicit type word wins. Failing that, a named opponent implies a
    fixture — which is the only signal the "Hawks vs Strikers" feed offers, and
    without it every game there files as a practice.

    None is deliberate: it routes to practices (a mis-filed practice is a
    smaller error than a missed game) and is surfaced through ``unknown_types``
    so the vocabulary can be extended.
    """
    if event_type:
        if _GAME_WORD.search(event_type):
            return True
        if _PRACTICE_WORD.search(event_type):
            return False
    if has_opponent:
        return True
    return None


@dataclass(frozen=True)
class SummaryParse:
    """What one coach-typed SUMMARY yielded. Any field may be None."""

    event_type: str | None = None
    opponent: str | None = None
    #: True home, False away, None not determinable. Never guessed — see
    #: normalize/title.py, which only marks away when positively known.
    home: bool | None = None
    venue_text: str | None = None
    #: A fixture was recognised but we could not tell which side is us, so no
    #: opponent is claimed. Surfaced so an activity alias can be added.
    unidentified: bool = False


def _matches_us(text: str, tokens: tuple[str, ...]) -> bool:
    """Whole-word, case-insensitive match against our own team's names."""
    cleaned = _WS.sub(" ", text).strip().casefold()
    if not cleaned:
        return False
    return any(
        re.search(rf"\b{re.escape(token.casefold())}\b", cleaned)
        for token in tokens
        if token and token.strip()
    )


def parse_summary(summary: str, tokens: tuple[str, ...] = ()) -> SummaryParse:
    """Read a SUMMARY by trying each known shape in turn.

    ``tokens`` are the strings identifying *our* team (``Activity.known_tokens``),
    which is what makes "Hawks vs Strikers" resolvable: whichever side is us
    fixes both the opponent and home/away. Without a token match the fixture is
    left unidentified rather than guessed at — naming ourselves as the opponent
    would be worse than naming nobody.
    """
    text = _WS.sub(" ", (summary or "").strip())
    if not text:
        return SummaryParse()

    parts = _VERSUS.split(text, maxsplit=1)

    # --- shape 1 & 2: a fixture ("X vs Y", "Game vs Y") --------------------
    if len(parts) == 2:
        left, right = parts[0].strip(), parts[1].strip()

        # A venue may still be tacked on after the opponent.
        venue_text = None
        if "-" in right:
            right, _, trailing = right.partition("-")
            right, venue_text = right.strip(), trailing.strip() or None

        prefix = _TYPE_PREFIX.match(left)
        stripped_left = left[prefix.end():].strip() if prefix else left
        event_type = (prefix.group(0).strip() if prefix else None) or None

        if not stripped_left:
            # "Game vs Jaguars" — the left side is purely a type label, so the
            # summary says nothing about who hosted.
            return SummaryParse(event_type, right or None, None, venue_text)
        if _matches_us(stripped_left, tokens):
            return SummaryParse(event_type, right or None, True, venue_text)
        if _matches_us(right, tokens):
            return SummaryParse(event_type, stripped_left or None, False, venue_text)
        # Neither side is recognisably us.
        return SummaryParse(event_type, None, None, venue_text, unidentified=True)

    # --- shape 3: "type - venue" -------------------------------------------
    head, _, tail = text.partition("-")
    if tail.strip():
        return SummaryParse(head.strip() or None, None, None, tail.strip())

    # --- shape 4: a bare label ("Practice", "First Practice") --------------
    return SummaryParse(text or None)


def content_hash(component) -> str:
    digest = hashlib.sha256()
    for key in HASH_FIELDS:
        digest.update(key.encode())
        digest.update(b"\x00")
        digest.update((_text(component, key) or "").encode())
        digest.update(b"\x1e")
    return digest.hexdigest()


@register("teamreach")
def parse_feed(
    data: str | bytes,
    activity: Activity,
    *,
    source_id: str,
    require_events: bool = True,
    default_duration_min: int | None = None,
) -> PollResult:
    """Parse a TeamReach ICS body into normalized events.

    ``default_duration_min`` fills in a missing DTEND. Left unset the event ends
    when it starts, matching the Player360 adapter — a zero-length event reads
    badly in a calendar, but inventing a duration is worse, so this is a
    deployment's decision rather than a default.
    """
    raw_bytes = data.encode() if isinstance(data, str) else data

    try:
        cal = Calendar.from_ical(raw_bytes)
    except Exception as exc:  # noqa: BLE001 - any parse failure is untrustworthy
        raise FeedError(f"could not parse feed for {source_id}: {exc}") from exc

    vevents = [c for c in cal.walk() if c.name == "VEVENT"]
    if require_events and not vevents:
        raise FeedError(
            f"feed for {source_id} contained no VEVENTs; refusing to treat "
            "an empty-but-valid feed as evidence of cancellation"
        )

    tokens = activity.known_tokens()
    events: list[Event] = []
    unknown_types: set[str] = set()
    unidentified: set[str] = set()

    for component in vevents:
        uid = _text(component, "UID")
        starts_at = _dt(component, "DTSTART")
        if not uid or starts_at is None:
            raise FeedError(f"VEVENT in {source_id} missing UID or DTSTART")

        ends_at = _dt(component, "DTEND")
        if ends_at is None:
            ends_at = starts_at + timedelta(minutes=default_duration_min or 0)

        raw_summary = _text(component, "SUMMARY") or ""
        parsed = parse_summary(raw_summary, tokens)

        if parsed.unidentified:
            # A fixture we could not place ourselves in. Recorded so the
            # operator can add an activity alias; no opponent is claimed.
            unidentified.add(raw_summary.strip())

        is_game = classify(parsed.event_type, has_opponent=bool(parsed.opponent))
        if is_game is None:
            if parsed.event_type:
                unknown_types.add(parsed.event_type)
            is_game = False

        # LOCATION wins when the feed has one: it is a field the coach filled in
        # deliberately, where the summary tail is whatever was left over after
        # parsing. Some teams publish neither.
        venue_source = _text(component, "LOCATION") or parsed.venue_text
        venue = None
        if venue_source:
            cleaned = clean_venue(venue_source)
            if cleaned:
                # Split the field designator off the venue name: "Riverview #2"
                # is one park, not a distinct place needing its own pin.
                name, field = venue_norm.split_field(cleaned)
                # No address and no coordinates anywhere in this feed — the
                # venue tables supply those, and a pin is never invented.
                venue = Venue(raw=cleaned, name=name or cleaned, field=field)

        # Some teams put a snack rota or a note here. Carry it, unless it just
        # repeats the title.
        body = _text(component, "DESCRIPTION")
        if body and body.strip() == raw_summary.strip():
            body = None

        events.append(
            Event(
                uid=uid,
                activity_id=activity.id,
                starts_at=starts_at,
                ends_at=ends_at,
                is_game=is_game,
                tz=activity.tz,
                venue=venue,
                opponent=parsed.opponent,
                home=parsed.home,
                detail=parsed.event_type,
                body=body,
                source_id=source_id,
                source_category=parsed.event_type,
                content_hash=content_hash(component),
            )
        )

    result = PollResult(
        source_id=source_id,
        events=events,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
    result.report("unknown_types", unknown_types)
    result.report("unidentified", unidentified)
    return result
