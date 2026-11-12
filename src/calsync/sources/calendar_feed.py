"""Calendar-feed adapter: a club, troop or school calendar's public iCal export.

What the feed does, and why each rule is here, is in
docs/connectors/calendar-feed.md.

These feeds are not fixtures. Every event is a non-game that keeps the feed's
own label, and the label is its category, which routing reads from the source's
config (`routing.collection_for`). The adapter only reports what it saw.

Unlike TeamReach, a Google export publishes repeating series as one VEVENT with
an RRULE, so they are expanded here, one event per occurrence, inside the sync
window.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import rrule as du
from icalendar import Calendar

from ..models import Activity, Event, PollResult, Venue
from ..normalize import venue as venue_norm
from . import register
from .teamreach import FeedError

KIND = "calendar-feed"

#: Content fields. STATUS is left out: a cancelled event never reaches the
#: content comparison (invariant 4).
HASH_FIELDS = ("DTSTART", "DTEND", "SUMMARY", "LOCATION", "DESCRIPTION")

#: As in the inbound expander: a season of daily meetings is about 300.
MAX_OCCURRENCES = 400

_FREQ = {"DAILY": du.DAILY, "WEEKLY": du.WEEKLY, "MONTHLY": du.MONTHLY, "YEARLY": du.YEARLY}
_DAYS = {"MO": du.MO, "TU": du.TU, "WE": du.WE, "TH": du.TH, "FR": du.FR, "SA": du.SA, "SU": du.SU}
_SUPPORTED = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST", "UNTIL", "COUNT"}


def label(summary: str | None) -> str:
    """The category an event belongs to: its title, spacing tidied."""
    return " ".join((summary or "").split())


def category_key(text: str) -> str:
    """How two titles are compared: ignoring case and spacing."""
    return label(text).casefold()


def _text(component, key: str) -> str | None:
    value = component.get(key)
    if value is None:
        return None
    return str(value).strip() or None


def _calendar_zone(cal, source_id: str) -> ZoneInfo | None:
    name = _text(cal, "X-WR-TIMEZONE")
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise FeedError(f"{source_id}: X-WR-TIMEZONE {name!r} is not a known zone") from exc


def _local(value, *, floating: ZoneInfo | None, uid: str, key: str):
    """``(naive wall-clock time, zone)`` for a DATE-TIME, or ``(date, None)``.

    Wall-clock time is what a series repeats in, so it is kept apart from the
    zone until each occurrence is anchored.
    """
    if not isinstance(value, datetime):
        return value, None
    if value.tzinfo is not None:
        return value.replace(tzinfo=None), value.tzinfo
    if floating is None:
        raise FeedError(f"VEVENT {uid}: {key} is a floating time and the calendar "
                        "names no X-WR-TIMEZONE")
    return value, floating


def _rule(component, uid: str, start_local, zone) -> du.rrule:
    rules = component.get("RRULE")
    if isinstance(rules, list):
        raise FeedError(f"VEVENT {uid}: more than one RRULE")
    parts = {k.upper(): v for k, v in dict(rules).items()}
    refused = sorted(set(parts) - _SUPPORTED)
    if refused:
        raise FeedError(f"VEVENT {uid}: RRULE part {', '.join(refused)} is not supported")
    if component.get("RDATE") is not None:
        raise FeedError(f"VEVENT {uid}: RDATE is not supported")

    freq = str(parts.get("FREQ", [""])[0]).upper()
    if freq not in _FREQ:
        raise FeedError(f"VEVENT {uid}: RRULE FREQ={freq or '(missing)'} is not supported")

    days = []
    for raw in parts.get("BYDAY", []):
        raw = str(raw).upper()
        name, ordinal = raw[-2:], raw[:-2]
        if name not in _DAYS:
            raise FeedError(f"VEVENT {uid}: RRULE BYDAY {raw!r} is not a weekday")
        try:
            days.append(_DAYS[name](int(ordinal)) if ordinal else _DAYS[name])
        except ValueError:
            raise FeedError(f"VEVENT {uid}: RRULE BYDAY {raw!r} has a bad ordinal") from None

    until = None
    if parts.get("UNTIL"):
        value = parts["UNTIL"][0]
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(zone).replace(tzinfo=None) if zone else \
                    value.replace(tzinfo=None)
            until = value
        else:
            # A date bound includes that whole day.
            until = datetime.combine(value, time.max)

    wkst = str(parts.get("WKST", ["MO"])[0]).upper()
    if wkst not in _DAYS:
        raise FeedError(f"VEVENT {uid}: RRULE WKST {wkst!r} is not a weekday")

    def ints(key):
        return [int(v) for v in parts.get(key, [])] or None

    start = start_local if isinstance(start_local, datetime) else \
        datetime.combine(start_local, time())
    try:
        return du.rrule(
            _FREQ[freq],
            dtstart=start,
            interval=int(parts.get("INTERVAL", [1])[0]),
            wkst=_DAYS[wkst],
            byweekday=days or None,
            bymonthday=ints("BYMONTHDAY"),
            bymonth=ints("BYMONTH"),
            count=int(parts["COUNT"][0]) if parts.get("COUNT") else None,
            until=until,
        )
    except (ValueError, TypeError) as exc:
        raise FeedError(f"VEVENT {uid}: RRULE cannot be read: {exc}") from exc


def _anchor(local, zone, tz: str) -> datetime:
    """A wall-clock occurrence as an instant. An all-day one is local midnight
    in the team's zone (invariant 13)."""
    if zone is None:
        return datetime(local.year, local.month, local.day, tzinfo=ZoneInfo(tz))
    return local.replace(tzinfo=zone)


def _occurrence_id(start: datetime, all_day: bool) -> str:
    if all_day:
        return start.strftime("%Y%m%d")
    return start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _override_key(override, *, floating, uid: str, tz: str) -> str:
    """The occurrence a RECURRENCE-ID override replaces, as its uid suffix."""
    prop = override.get("RECURRENCE-ID")
    if prop.params.get("RANGE"):
        raise FeedError(f"VEVENT {uid}: RECURRENCE-ID RANGE is not supported")
    original, zone = _local(prop.dt, floating=floating, uid=uid, key="RECURRENCE-ID")
    return _occurrence_id(_anchor(original, zone, tz), not isinstance(original, datetime))


def _excluded(component, *, zone, floating, uid) -> set:
    """EXDATEs as wall-clock values in the series' zone (or dates), to compare
    with the rule's. An EXDATE may be written in UTC for a local series."""
    out = set()
    raw = component.get("EXDATE")
    for group in raw if isinstance(raw, list) else [raw] if raw is not None else []:
        for item in group.dts:
            value = item.dt
            if isinstance(value, datetime) and value.tzinfo is not None and zone is not None:
                value = value.astimezone(zone)
            local, _ = _local(value, floating=floating, uid=uid, key="EXDATE")
            out.add(local)
    return out


def _hash(starts_at: datetime, ends_at: datetime, component) -> str:
    """Over the occurrence's own times, so each occurrence of a series hashes
    apart and an edit to one is a change to that one only."""
    digest = hashlib.sha256()
    values = (starts_at.isoformat(), ends_at.isoformat(),
              _text(component, "SUMMARY"), _text(component, "LOCATION"),
              _text(component, "DESCRIPTION"))
    for key, value in zip(HASH_FIELDS, values):
        digest.update(key.encode())
        digest.update(b"\x00")
        digest.update((value or "").encode())
        digest.update(b"\x1e")
    return digest.hexdigest()


def _venue(component):
    venue = venue_norm.parse(_text(component, "LOCATION"))
    if venue is None or not venue.name:
        return venue
    # "Anchorhead #2" is one park, not a place of its own (invariant 17).
    name, field = venue_norm.split_field(venue.name)
    if not field:
        return venue
    return Venue(raw=venue.raw, name=name or venue.name, address=venue.address,
                 field=field)


def _times(component, *, floating, uid, tz):
    """``(start, end, all_day, start_local, zone)`` for one VEVENT as published."""
    prop = component.get("DTSTART")
    if prop is None:
        raise FeedError(f"VEVENT {uid}: no DTSTART")
    start_local, zone = _local(prop.dt, floating=floating, uid=uid, key="DTSTART")
    all_day = not isinstance(start_local, datetime)
    starts_at = _anchor(start_local, zone, tz)

    end_prop = component.get("DTEND")
    if end_prop is not None:
        end_local, end_zone = _local(end_prop.dt, floating=floating, uid=uid, key="DTEND")
        length = _anchor(end_local, end_zone, tz) - starts_at
    elif component.get("DURATION") is not None:
        length = component.get("DURATION").dt
    else:
        length = timedelta(days=1) if all_day else timedelta(0)
    return starts_at, max(length, timedelta(0)), all_day, start_local, zone


def _event(component, *, uid: str, starts_at, length, all_day, activity, source_id) -> Event:
    summary = label(_text(component, "SUMMARY"))
    if not summary:
        raise FeedError(f"VEVENT {uid}: no SUMMARY, so no category to route it by")
    body = _text(component, "DESCRIPTION")
    ends_at = starts_at + length
    if all_day:
        # Keep a multi-day event's end on local midnight across a DST change.
        days = max(length.days, 1)
        end = (starts_at.replace(tzinfo=None) + timedelta(days=days))
        ends_at = end.replace(tzinfo=starts_at.tzinfo)
    return Event(
        uid=uid,
        activity_id=activity.id,
        starts_at=starts_at,
        ends_at=ends_at,
        is_game=False,
        tz=activity.tz,
        venue=_venue(component),
        detail=summary,
        body=body if body and body != summary else None,
        all_day=all_day,
        source_id=source_id,
        source_category=summary,
        content_hash=_hash(starts_at, ends_at, component),
        cancelled=(_text(component, "STATUS") or "").upper() == "CANCELLED",
    )


@register(KIND, windowed=True)
def parse_feed(
    data: str | bytes,
    activity: Activity,
    *,
    source_id: str,
    config: dict | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> PollResult:
    """Parse a public calendar export into one event per occurrence.

    ``window`` is the sync's ``(earliest, latest)``. Only occurrences starting
    inside it are produced, which is what bounds a series with no end.
    """
    raw_bytes = data.encode() if isinstance(data, str) else data
    try:
        cal = Calendar.from_ical(raw_bytes)
    except Exception as exc:  # noqa: BLE001 - any parse failure is untrustworthy
        raise FeedError(f"could not parse feed for {source_id}: {exc}") from exc

    vevents = [c for c in cal.walk() if c.name == "VEVENT"]
    if not vevents:
        raise FeedError(
            f"feed for {source_id} contained no VEVENTs; refusing to treat "
            "an empty-but-valid feed as evidence of cancellation"
        )
    floating = _calendar_zone(cal, source_id)

    masters: dict[str, object] = {}
    overrides: dict[str, list] = {}
    for component in vevents:
        uid = _text(component, "UID")
        if not uid:
            raise FeedError(f"a VEVENT in {source_id} has no UID")
        if component.get("RECURRENCE-ID") is not None:
            overrides.setdefault(uid, []).append(component)
        elif uid in masters:
            raise FeedError(f"VEVENT {uid} appears twice in {source_id}")
        else:
            masters[uid] = component

    events: list[Event] = []
    for uid, component in masters.items():
        starts_at, length, all_day, start_local, zone = _times(
            component, floating=floating, uid=uid, tz=activity.tz)

        if component.get("RRULE") is None:
            events.append(_event(component, uid=uid, starts_at=starts_at, length=length,
                                 all_day=all_day, activity=activity, source_id=source_id))
            continue

        rule = _rule(component, uid, start_local, zone)
        excluded = _excluded(component, zone=zone, floating=floating, uid=uid)
        # Overrides keyed by the occurrence they replace.
        replaced: dict[str, object] = {}
        for override in overrides.pop(uid, []):
            replaced[_override_key(override, floating=floating, uid=uid,
                                   tz=activity.tz)] = override

        produced = 0
        for slot in _slots(rule, window, zone, all_day, activity.tz):
            local = slot if not all_day else slot.date()
            if local in excluded:
                continue
            produced += 1
            if produced > MAX_OCCURRENCES:
                raise FeedError(f"VEVENT {uid}: the series produces more than "
                                f"{MAX_OCCURRENCES} occurrences in the sync window")
            at = _anchor(local, zone, activity.tz)
            key = _occurrence_id(at, all_day)
            occurrence_uid = f"{uid}/{key}"
            override = replaced.pop(key, None)
            if override is not None:
                o_start, o_length, o_all_day, _, _ = _times(
                    override, floating=floating, uid=uid, tz=activity.tz)
                events.append(_event(override, uid=occurrence_uid, starts_at=o_start,
                                     length=o_length, all_day=o_all_day,
                                     activity=activity, source_id=source_id))
            else:
                events.append(_event(component, uid=occurrence_uid, starts_at=at,
                                     length=length, all_day=all_day,
                                     activity=activity, source_id=source_id))

        # An override whose original slot is outside the window, moved into it.
        for key, override in replaced.items():
            o_start, o_length, o_all_day, _, _ = _times(
                override, floating=floating, uid=uid, tz=activity.tz)
            if window is None or window[0] <= o_start <= window[1]:
                events.append(_event(override, uid=f"{uid}/{key}", starts_at=o_start,
                                     length=o_length, all_day=o_all_day,
                                     activity=activity, source_id=source_id))

    # An override whose series is not in this feed. Keyed by its RECURRENCE-ID
    # like any other, so its uid holds when the series appears.
    for uid, orphans in overrides.items():
        for override in orphans:
            key = _override_key(override, floating=floating, uid=uid, tz=activity.tz)
            o_start, o_length, o_all_day, _, _ = _times(
                override, floating=floating, uid=uid, tz=activity.tz)
            if window is None or window[0] <= o_start <= window[1]:
                events.append(_event(override, uid=f"{uid}/{key}", starts_at=o_start,
                                     length=o_length, all_day=o_all_day,
                                     activity=activity, source_id=source_id))

    result = PollResult(
        source_id=source_id,
        events=events,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
    # Counted under the first spelling seen, so "padawan meeting" and
    # "Padawan Meeting" are one category.
    spelled: dict[str, str] = {}
    for event in events:
        name = spelled.setdefault(category_key(event.source_category), event.source_category)
        result.categories[name] = result.categories.get(name, 0) + 1
    return result


def _slots(rule: du.rrule, window, zone, all_day: bool, tz: str):
    """Naive wall-clock starts of the rule inside the window, in order.

    The window is in instants; a day's margin either side covers any zone's
    offset, and each slot is then checked as an instant.
    """
    if window is None:
        # Unbounded here; the caller's cap stops a rule with no end.
        yield from rule
        return
    anchor_zone = ZoneInfo(tz) if zone is None else zone
    low = window[0].astimezone(anchor_zone).replace(tzinfo=None) - timedelta(days=1)
    high = window[1].astimezone(anchor_zone).replace(tzinfo=None) + timedelta(days=1)
    for slot in rule.xafter(low, inc=True):
        if slot > high:
            break
        at = slot.replace(tzinfo=anchor_zone)
        if window[0] <= at <= window[1]:
            yield slot
