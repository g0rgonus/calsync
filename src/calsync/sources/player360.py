"""Player360 ICS adapter.

Findings and traps documented in docs/sources/player360.md. The two that shape
this code:

- SEQUENCE == unix(LAST-MODIFIED), and every event is touched 2-3s after its
  DTEND. Those fields churn on events that did not change, so change detection
  uses our own content hash and upstream SEQUENCE is never propagated.
- Cancellation is signalled only by an event disappearing, which makes a
  truncated response indistinguishable from a cancelled season. Parsing is
  therefore strict, and the disappearance guard lives in diff.py.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar

from .. import models
from ..models import Activity, Event, PollResult, Venue
from ..normalize import summary as summary_norm
from ..normalize import venue as venue_norm
from . import register

#: CATEGORIES values that route to the Games calendar. Treat as an open
#: vocabulary: anything unseen falls to Practices (the safe default) and is
#: surfaced by `unknown_categories` so the mapping can be extended.
GAME_CATEGORIES = frozenset({"match", "game", "scrimmage", "tournament", "friendly"})

#: Fields whose change means the event really changed. Note the absence of
#: SEQUENCE, LAST-MODIFIED and DTSTAMP.
HASH_FIELDS = ("DTSTART", "DTEND", "SUMMARY", "LOCATION", "CATEGORIES", "DESCRIPTION")


class FeedError(RuntimeError):
    """The feed could not be trusted. Never treat this as 'everything cancelled'."""


def _text(component, key: str) -> str | None:
    value = component.get(key)
    if value is None:
        return None
    return str(value).strip() or None


def _categories(component) -> list[str]:
    raw = component.get("CATEGORIES")
    if raw is None:
        return []
    cats = getattr(raw, "cats", None)
    if cats is not None:
        return [str(c).strip().lower() for c in cats if str(c).strip()]
    return [c.strip().lower() for c in str(raw).split(",") if c.strip()]


def _dt(component, key: str) -> datetime | None:
    prop = component.get(key)
    if prop is None:
        return None
    value = prop.dt
    if not isinstance(value, datetime):
        return None  # a date, not an instant — `_date` picks these up
    if value.tzinfo is None:
        # The feed publishes Z-suffixed UTC. A naive value means the parse
        # lost the offset, and guessing a zone here would shift every render.
        raise FeedError(f"{key} has no timezone: {value!r}")
    return value.astimezone(timezone.utc)


def _date(component, key: str) -> date | None:
    """A `VALUE=DATE` property. Not observed on this feed, and handled anyway:
    the same coach behaviour produced them on TeamReach, and one adapter
    crashing on what the other supports is a trap rather than a policy."""
    prop = component.get(key)
    if prop is None:
        return None
    value = prop.dt
    if isinstance(value, datetime) or not isinstance(value, date):
        return None
    return value


def content_hash(component) -> str:
    """Hash only the fields we care about.

    Deliberately excludes SEQUENCE/LAST-MODIFIED/DTSTAMP: Player360 bumps
    those when an event *ends*, which would otherwise re-push every event on
    the evening it happened.
    """
    digest = hashlib.sha256()
    for key in HASH_FIELDS:
        digest.update(key.encode())
        digest.update(b"\x00")
        digest.update((_text(component, key) or "").encode())
        digest.update(b"\x1e")
    return digest.hexdigest()


@register("player360")
def parse_feed(
    data: str | bytes,
    activity: Activity,
    *,
    source_id: str,
    require_events: bool = True,
    config: dict | None = None,
) -> PollResult:
    """Parse an ICS body into normalized events.

    Strict on structure: an unparseable or empty feed raises rather than
    returning zero events, because downstream a zero-event result is
    indistinguishable from every event having been cancelled.
    """
    if isinstance(data, str):
        raw_bytes = data.encode()
    else:
        raw_bytes = data

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
    # Same two config keys as the TeamReach adapter, applied to CATEGORIES
    # rather than to a summary word. One vocabulary to learn, not two.
    vocabulary = config or {}
    extra_games = {str(w).strip().casefold() for w in (vocabulary.get("game_words") or ())}
    extra_practices = {
        str(w).strip().casefold() for w in (vocabulary.get("practice_words") or ())
    }

    events: list[Event] = []
    unknown_categories: set[str] = set()

    for component in vevents:
        uid = _text(component, "UID")
        starts_at = _dt(component, "DTSTART")
        ends_at = _dt(component, "DTEND")
        all_day = False

        if starts_at is None:
            day = _date(component, "DTSTART")
            if day is not None:
                # Local midnight in the activity's timezone, so everything
                # downstream sees an ordinary instant and only the render has
                # to know. See `Event.all_day`.
                all_day = True
                starts_at = datetime(
                    day.year, day.month, day.day, tzinfo=ZoneInfo(activity.tz)
                )
                end_day = _date(component, "DTEND")
                ends_at = (
                    datetime(end_day.year, end_day.month, end_day.day,
                             tzinfo=ZoneInfo(activity.tz))
                    if end_day is not None else starts_at + timedelta(days=1)
                )

        if not uid or starts_at is None:
            # Name the event and the actual problem. "missing UID or DTSTART"
            # sent somebody to the logs for a feed where both were present.
            raw_start = component.get("DTSTART")
            detail = (
                f"DTSTART is {raw_start.to_ical().decode()!r}, which is neither a "
                "timestamp nor a date" if raw_start is not None else "no DTSTART"
            ) if uid else "no UID"
            raise FeedError(
                f"VEVENT {uid or '<no uid>'} in {source_id} cannot be read: {detail}"
            )

        if ends_at is None:
            ends_at = starts_at

        cats = _categories(component)
        is_game = any(c in GAME_CATEGORIES or c in extra_games for c in cats)
        unresolved: list[str] = []
        if cats and not is_game:
            unrecognised = {
                c for c in cats
                if c not in ("practice", "training") and c not in extra_practices
            }
            unknown_categories.update(unrecognised)
            # Carried on the event as well as reported for the source: an
            # unrecognised category falls to practices, and if it was a
            # tournament that is the wrong calendar, which costs a move to
            # correct rather than an update.
            if unrecognised:
                unresolved.append(models.UNKNOWN_CATEGORY)

        raw_summary = _text(component, "SUMMARY") or ""
        opponent, detail = summary_norm.parse(
            raw_summary, tokens=tokens, age_group=activity.age_group
        )

        venue: Venue | None = venue_norm.parse(_text(component, "LOCATION"))
        home = venue_norm.matches_home(venue, activity.home_venue)

        # League matches repeat SUMMARY verbatim in DESCRIPTION; carrying that
        # through would put the title in the body of every game.
        description = _text(component, "DESCRIPTION")
        if description and description.strip() == raw_summary.strip():
            description = None

        events.append(
            Event(
                uid=uid,
                activity_id=activity.id,
                starts_at=starts_at,
                ends_at=ends_at,
                is_game=is_game,
                tz=activity.tz,
                venue=venue,
                opponent=opponent,
                home=home,
                detail=detail,
                body=description,
                url=_text(component, "URL"),
                source_id=source_id,
                source_category=cats[0] if cats else None,
                all_day=all_day,
                content_hash=content_hash(component),
                # Carried, not hashed. `content_hash` above deliberately omits
                # it; see `Event.upstream_modified_at` for the one thing it is
                # good for.
                upstream_modified_at=_dt(component, "LAST-MODIFIED"),
                unresolved=tuple(unresolved),
            )
        )

    result = PollResult(
        source_id=source_id,
        events=events,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
    result.report("unknown_categories", unknown_categories)
    return result
