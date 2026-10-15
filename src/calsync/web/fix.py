"""The page for fixing one event, as data the template renders.

Kept out of `app.py` for `gate.py`'s reason: what the page says and how a
posted form becomes overrides are the parts worth testing, and neither needs a
request to test.

The form is filled in with what the calendar will show, and it carries no
per-field switches. So the one rule everything here exists to keep is that **a
field left alone stays the team app's**: each part of the form is compared with
what the feed itself says, and becomes an override only if it differs. A part
put back to the feed's value lifts the override it had.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .. import overlay, repo, retire
from ..normalize import title as title_norm
from ..settings import Settings

#: What each source kind is called by the people who use it. Anything else is
#: "the team app", which is what everybody calls it anyway.
APP_NAMES = {"player360": "360Player", "teamreach": "TeamReach", "inbound": "the sender"}

#: How each unit is named in a sentence: "Time and place changed by Dan".
LABELS = {
    "time": "time", "location": "place", "opponent": "opponent",
    "detail": "name", "notes": "notes",
}


def app_name(kind: str) -> str:
    return APP_NAMES.get(kind, "the team app")


@dataclass
class Plan:
    """What a posted form asks for, unit by unit."""

    sets: dict[str, dict] = field(default_factory=dict)
    removes: list[str] = field(default_factory=list)
    #: ``(from, to)`` local dates, when the time moves the event to another day.
    moved_day: tuple[date, date] | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.sets and not self.removes


# --- reading what is there ---------------------------------------------------


@dataclass
class Subject:
    """One event, as the page needs it: what the feed said, what is saved now."""

    item: repo.StoredEvent
    state: repo.EventState
    source_kind: str
    overrides: dict[str, repo.Override]
    #: A place by any name it is known by, from the venue table — so typing
    #: "Kingsmere" brings Kingsmere's address with it.
    known_place: Callable[[str], object] = lambda name: None

    @property
    def source(self):
        return self.item.source_event

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.source.tz)

    @property
    def current(self):
        """The event as the calendar shows it once every saved fix has landed."""
        return overlay.apply(self.source, {
            unit: {"value": o.value, "set_at": o.set_at}
            for unit, o in self.overrides.items()
        })

    def source_value(self, unit: str) -> dict:
        return _value(self.source, unit, self.state.starts_at)

    def upcoming(self, now: datetime) -> bool:
        # Both starts, for `retire.py`'s reason: changing either record of a
        # game that has happened rewrites the history of a game that was played.
        return (retire.upcoming(self.state.shown_starts_at, now)
                and retire.upcoming(self.state.starts_at, now))


def subject(conn, uid: str) -> Subject | None:
    item = repo.stored_event(conn, uid)
    state = repo.event_state(conn, uid)
    if item is None or state is None:
        return None
    source = repo.get_source(conn, state.source_id)
    return Subject(
        item=item, state=state, source_kind=source.kind if source else "",
        overrides=repo.overrides_of(conn, uid),
        known_place=lambda name: repo.resolve_venue_alias(conn, name),
    )


def _value(event, unit: str, starts_at: str | None = None) -> dict:
    """One unit of an event, in the stored representation."""
    content = {**repo.content_of(event), "starts_at": starts_at or event.starts_at.isoformat()}
    return {column: content[column] for column in overlay.UNITS[unit]}


# --- the page ----------------------------------------------------------------


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%-H:%M")


def _day(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%a %-d %b")


def _when(event, zone: ZoneInfo) -> str:
    if event.all_day:
        return f"{_day(event.starts_at, zone)}, all day"
    return (f"{_day(event.starts_at, zone)}, {_clock(event.starts_at, zone)}"
            f"–{_clock(event.ends_at, zone)}")


def _place(event) -> str:
    venue = event.venue
    if venue is None:
        return ""
    name = venue.name or venue.raw
    return f"{name}, field {venue.field}" if venue.field else name


def page(conn, subject: Subject, *, now: datetime, back: str,
         form: dict | None = None) -> dict:
    """Everything `event_fix.tpl` renders, for one event."""
    source, current, zone = subject.source, subject.current, subject.zone
    settings = Settings.load(conn)
    activity = repo.get_activity(conn, subject.item.activity_id)
    child = repo.get_child(conn, activity.child_id)

    def unit(key: str, team: str, now_text: str, **extra) -> dict:
        saved = subject.overrides.get(key)
        stamp = repo.parse_db_stamp(saved.set_at) if saved else None
        return {
            "key": key, "label": LABELS[key], "team_says": team, "now": now_text,
            "fixed": saved is not None,
            "by": saved.actor if saved else None,
            "on": _day(stamp, zone) if stamp else "",
            "reason": saved.reason if saved else "",
            **extra,
        }

    units = {
        "time": unit(
            "time", _when(source, zone), _when(current, zone),
            team_says_clock=_clock(source.starts_at, zone),
            now_clock=_clock(current.starts_at, zone),
            team_says_day=_day(source.starts_at, zone),
            now_day=_day(current.starts_at, zone),
            all_day=current.all_day,
        ),
        "location": unit("location", _place(source), _place(current)),
        "opponent": unit("opponent", source.opponent or "", current.opponent or ""),
        "detail": unit("detail", source.detail or "", current.detail or ""),
        "notes": unit("notes", source.body or "", current.body or ""),
    }

    local = current.starts_at.astimezone(zone)
    defaults = {
        "date": local.date().isoformat(),
        "start": "" if current.all_day else local.strftime("%H:%M"),
        "end": "" if current.all_day else current.ends_at.astimezone(zone).strftime("%H:%M"),
        "all_day": current.all_day,
        "venue_name": (current.venue.name or current.venue.raw) if current.venue else "",
        "venue_field": (current.venue.field or "") if current.venue else "",
        # Not prefilled: an address left over from the old place is exactly
        # the wrong pin when somebody types a new one. A known place brings its
        # own; an unchanged one keeps the feed's.
        "venue_address": "",
        "opponent": current.opponent or "",
        "detail": current.detail or "",
        "notes": current.body or "",
    }
    app = app_name(subject.source_kind)
    return {
        "uid": subject.item.event.uid,
        "back": back,
        "title": title_norm.render(current, activity, [child], settings),
        "noun": "game" if current.is_game else "practice",
        "is_game": current.is_game,
        "when_text": local.strftime("%A %-d %B"),
        "team_text": f"{child.name}’s {activity.name} team",
        "app": app,
        "App": app[:1].upper() + app[1:],
        "upcoming": subject.upcoming(now),
        # What is saved and what is on the calendar can differ for as long as
        # the calendar server was unreachable when the fix was saved.
        "pending": overlay.values(subject.item.overrides) != {
            u: o.value for u, o in subject.overrides.items()
        },
        "units": units,
        "form": {**defaults, **(form or {})},
        "venues": [r["canonical_name"] for r in repo.list_venues(conn)],
        "actor": (form or {}).get("actor") or repo.last_override_actor(conn) or "",
        "alerts": [],
    }


def calendar_note(item: repo.StoredEvent, overrides: dict[str, repo.Override]) -> str | None:
    """The line under a fixed event in the agenda: what changed, by whom, why."""
    applied = list((item.overrides or {}).keys())
    if not applied:
        return None
    labels = [LABELS[u] for u in overlay.UNITS if u in applied]
    what = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    latest = max(overrides.values(), key=lambda o: o.set_at, default=None)
    if latest is None:
        return f"{what.capitalize()} changed by hand."
    who = latest.actor or "someone"
    return f"{what.capitalize()} changed by {who}: “{latest.reason}”"


# --- turning a posted form into overrides -------------------------------------


def plan(subject: Subject, form: dict) -> Plan:
    """Compare a posted form with the feed, unit by unit."""
    result = Plan()
    wanted: dict[str, dict] = {}

    time_value = _time_from(subject, form, result)
    if time_value is not None:
        wanted["time"] = time_value
    if "venue_name" in form:
        location = _location_from(subject, form, result)
        if location is not None:
            wanted["location"] = location
    for unit, column, name in (("opponent", "opponent", "opponent"),
                               ("detail", "detail", "detail"),
                               ("notes", "body", "notes")):
        if name in form:
            # Browsers send a textarea's line breaks as CRLF.
            wanted[unit] = {column: form[name].replace("\r\n", "\n")}

    for unit, value in wanted.items():
        try:
            value = overlay.normalize(unit, value)
        except overlay.OverrideError as exc:
            result.errors.append(str(exc))
            continue
        saved = subject.overrides.get(unit)
        if overlay.same(unit, value, subject.source_value(unit)):
            if saved is not None:
                result.removes.append(unit)
        elif saved is None or not overlay.same(unit, value, saved.value):
            result.sets[unit] = value

    if "time" in result.sets:
        zone = subject.zone
        was = subject.current.starts_at.astimezone(zone).date()
        becomes = datetime.fromisoformat(result.sets["time"]["starts_at"]).astimezone(zone).date()
        if was != becomes:
            result.moved_day = (was, becomes)
    return result


def _time_from(subject: Subject, form: dict, result: Plan) -> dict | None:
    if "date" not in form:
        return None
    source, zone = subject.source, subject.zone
    try:
        day = date.fromisoformat(form["date"])
    except ValueError:
        result.errors.append("Pick the day it is on.")
        return None

    if form.get("all_day"):
        # As long as the feed had it, if the feed had it as all-day: a
        # two-day tournament moved is still two days. One day otherwise.
        span = (source.ends_at - source.starts_at).days if source.all_day else 1
        starts = datetime.combine(day, time(), zone)
        ends = datetime.combine(day + timedelta(days=max(span, 1)), time(), zone)
        return {"starts_at": starts.isoformat(), "ends_at": ends.isoformat(), "all_day": 1}

    try:
        start = time.fromisoformat(form.get("start") or "")
    except ValueError:
        result.errors.append("Give a start time, or tick “All day”.")
        return None
    starts = datetime.combine(day, start, zone)
    if form.get("end"):
        try:
            ends = datetime.combine(day, time.fromisoformat(form["end"]), zone)
        except ValueError:
            result.errors.append("The end time isn’t a time.")
            return None
    else:
        # No end given: keep the length the feed gave it, or an hour for an
        # event that never had one.
        length = (source.ends_at - source.starts_at) if not source.all_day else timedelta(hours=1)
        ends = starts + length
    if ends < starts:
        result.errors.append("It ends before it starts — check the end time.")
        return None
    return {"starts_at": starts.isoformat(), "ends_at": ends.isoformat(), "all_day": 0}


def _location_from(subject: Subject, form: dict, result: Plan) -> dict | None:
    name = " ".join((form.get("venue_name") or "").split())
    field_ = (form.get("venue_field") or "").strip() or None
    typed_address = (form.get("venue_address") or "").strip() or None
    source_venue = subject.source.venue
    if not name:
        if source_venue is None:
            return None
        result.errors.append("Say where it is, or put back the place it had.")
        return None

    address = typed_address
    if address is None and source_venue is not None and overlay._squash(name) in {
        overlay._squash(source_venue.name), overlay._squash(source_venue.raw),
    }:
        # The same place the feed named: keep the address the feed (or the
        # venue table) gave it, rather than reading a blank box as "none".
        address = source_venue.address
    elif address is None:
        known = subject.known_place(name)
        if known is not None:
            name, address = known.name or name, known.address
    return {"venue_name": name, "venue_field": field_, "venue_address": address}
