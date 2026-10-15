"""A person's correction to one event, laid over what the feed said.

Coaches do not always fix an event upstream, so somebody fixes it here. The
correction has to survive every poll of a feed that goes on publishing the old
value, and that is a question of which layer each writer owns rather than of
which value wins (docs/API.md, "What calsync remembers"):

    calendar content = source layer  +  overlay

A poll writes the source layer — `event_content`, and `event_state.starts_at`
— and nothing else. A person writes the overlay — `event_overrides` — and
nothing else. So a poll *cannot* revert a correction: it never touches the
layer the correction lives in.

Three things here are easy to undo by accident:

- **The feed's `content_hash` passes through untouched.** `diff.diff_poll`
  reads nothing but a uid and that hash, so an overridden event is the same
  event to the diff and to the disappearance guard as it was before anybody
  touched it. The guard must only ever weigh what the feed said.
- **A correction is a change the hash cannot see**, like a venue gaining an
  address. So the sync loop compares what it last wrote — the stored source
  layer plus `event_state.applied_overlay` — against what it would write now,
  through :func:`project`, the same way it already checks placement and stored
  content independently of the hash.
- **The comparison is of effective content, not of which overrides exist.**
  Removing an override that says what the feed now says changes nothing on
  anybody's phone, and must not cost a rewrite of the calendar.

A unit is what a coach changes together. ``time`` is start, end and all-day as
one, because a game moved an hour moves both ends; ``location`` is the venue
and the field within it. A warm-up carries no override of its own: it is
derived from its game (`warmup.py`), so it follows the game's corrected start
and venue for free.

Values are stored in `event_content`'s own column representation, and this
module is the only translation between that and an `Event`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import Event, Venue
from .normalize.venue import split_field

#: Each unit, and the receipt columns it replaces. ``starts_at`` is not an
#: `event_content` column — it lives on `event_state` — but it is part of the
#: receipt :func:`project` composes, so it is named here like the others.
UNITS: dict[str, tuple[str, ...]] = {
    "time": ("starts_at", "ends_at", "all_day"),
    "location": ("venue_raw", "venue_name", "venue_address", "venue_field"),
    "opponent": ("opponent",),
    "detail": ("detail",),
    "notes": ("body",),
}

#: ``{unit: {"value": {column: value}, "set_at": str}}`` — the shape
#: `repo.overrides_for` returns and `event_state.applied_overlay` stores.
Overlay = dict[str, dict]


class OverrideError(ValueError):
    """An override that cannot be stored as given."""


def normalize(unit: str, value: dict) -> dict:
    """``value``, checked and in the stored representation, or raise.

    Refused rather than repaired, for `onboarding.templatise`'s reason: a value
    silently reshaped on the way in surfaces weeks later as an event in the
    wrong place, with nothing to point at.
    """
    if unit not in UNITS:
        raise OverrideError(
            f"{unit!r} is not something an event can be corrected on; "
            f"expected one of {', '.join(UNITS)}"
        )
    if not isinstance(value, dict):
        raise OverrideError(f"a {unit} override is a mapping of fields")
    unknown = set(value) - set(UNITS[unit])
    if unknown:
        raise OverrideError(
            f"a {unit} override does not carry {', '.join(sorted(unknown))}"
        )
    if unit == "time":
        return _time(value)
    if unit == "location":
        return _location(value)
    (column,) = UNITS[unit]
    return {column: _text(value.get(column))}


def _instant(value, name: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None:
        raise OverrideError(f"{name} must be an ISO 8601 date-time, got {value!r}")
    # The same rule `Event.__post_init__` enforces, for the same reason: a
    # naive time is a time that lost its offset, and it shifts every render.
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise OverrideError(f"{name} must carry an offset, got {value!r}")
    return moment


def _time(value: dict) -> dict:
    missing = {"starts_at", "ends_at"} - set(value)
    if missing:
        raise OverrideError(f"a time override needs {', '.join(sorted(missing))}")
    starts = _instant(value["starts_at"], "starts_at")
    ends = _instant(value["ends_at"], "ends_at")
    if ends < starts:
        raise OverrideError("the end of an event cannot be before its start")
    return {
        "starts_at": starts.isoformat(),
        "ends_at": ends.isoformat(),
        "all_day": int(bool(value.get("all_day", 0))),
    }


def _location(value: dict) -> dict:
    name = _text(value.get("venue_name"))
    if not name:
        raise OverrideError("a location override needs the venue's name")
    # Venue identity excludes the field designator, everywhere a person could
    # type one (`normalize/venue.split_field`). "Kingsmere #2" as a name would
    # be a different venue from Kingsmere, with its own pin.
    bare, designator = split_field(name)
    if designator:
        raise OverrideError(
            f"{name!r} names a field within the venue; give the venue as "
            f"{bare!r} and the field as {designator!r}"
        )
    return {
        "venue_raw": _text(value.get("venue_raw")) or name,
        "venue_name": name,
        "venue_address": _text(value.get("venue_address")),
        "venue_field": _text(value.get("venue_field")),
    }


def _text(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OverrideError(f"expected text, got {value!r}")
    return value.strip() or None


def values(overlay: Overlay | None) -> dict[str, dict]:
    """``{unit: value}``, without when each was set.

    What decides whether two overlays put the same thing on the calendar.
    """
    return {unit: entry["value"] for unit, entry in (overlay or {}).items()}


def project(content: dict, starts_at: str, overlay: Overlay | None) -> dict:
    """The receipt of what is on the calendar: a source layer with ``overlay`` on top.

    ``content`` is in `repo.content_of`'s form and ``starts_at`` is the source
    start. Used on both sides of the sync loop's comparison — what was last
    written and what would be written now — so it is the one definition of
    "the same event" that an override can change.
    """
    projected = {**content, "starts_at": starts_at}
    for value in values(overlay).values():
        projected.update(value)
    return projected


def apply(event: Event, overlay: Overlay | None) -> Event:
    """``event`` as corrected, leaving ``event`` itself untouched.

    The source event is still needed after this — it is what `event_content`
    records — so this returns a copy rather than editing in place, unlike
    `sync._enrich_venue`. Nothing that identifies the event changes: not the
    uid, and not ``content_hash``, which is what keeps the diff about the feed.
    """
    fields: dict = {}
    for unit, value in values(overlay).items():
        if unit == "time":
            fields["starts_at"] = datetime.fromisoformat(value["starts_at"])
            fields["ends_at"] = datetime.fromisoformat(value["ends_at"])
            fields["all_day"] = bool(value["all_day"])
        elif unit == "location":
            # By value, as an event carries its venue, and with no coordinates
            # — none are emitted. A deleted venue row costs a pin here exactly
            # as it does for an event that was never corrected.
            fields["venue"] = Venue(
                raw=value["venue_raw"] or value["venue_name"],
                name=value["venue_name"],
                address=value["venue_address"],
                field=value["venue_field"],
            )
        elif unit == "opponent":
            fields["opponent"] = value["opponent"]
        elif unit == "detail":
            fields["detail"] = value["detail"]
        elif unit == "notes":
            fields["body"] = value["body"]
    return replace(event, **fields) if fields else event


def same(unit: str, a: dict | None, b: dict | None) -> bool:
    """Do two values of one unit put the same thing on the calendar?

    Times compare as instants, because an offset and a ``Z`` spell one moment
    differently. Text compares with whitespace collapsed and case kept: case
    shows on a phone, and a coach's doubled space does not. Notes keep their
    line breaks (`_lines`). ``venue_raw`` is
    left out — it is the string a feed happened to type, not where the event is.
    """
    if a is None or b is None:
        return a is None and b is None
    if unit == "time":
        return (
            bool(a["all_day"]) == bool(b["all_day"])
            and datetime.fromisoformat(a["starts_at"]) == datetime.fromisoformat(b["starts_at"])
            and datetime.fromisoformat(a["ends_at"]) == datetime.fromisoformat(b["ends_at"])
        )
    flatten = _lines if unit == "notes" else _squash
    return all(
        flatten(a.get(column)) == flatten(b.get(column))
        for column in UNITS[unit]
        if column != "venue_raw"
    )


def _lines(value) -> str | None:
    """Notes keep their line breaks — a list somebody typed one item per line
    is a different note run together — and lose only trailing spaces."""
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").strip()
    return "\n".join(line.rstrip() for line in text.split("\n")) or None


def _squash(value) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split()) or None


#: What a poll concludes about one override, against what the source says now.
CONVERGED = "converged"    # the source now says what the fix says: lift it
UNCHANGED = "unchanged"    # the source still says what it said when the fix was set
CHANGED = "changed"        # the source says something else again: ask a person


def matches(unit: str, a: dict | None, b: dict | None, *, place=None) -> bool:
    """`same`, except that two names for one place are one place.

    ``place`` resolves a name through the venue table (`repo.resolve_venue_alias`).
    "Kingsmere" and "Kingsmere Meadow Park" are the same park once an alias says
    so, and a feed catching up with a corrected venue under a different
    spelling has still caught up. When either side is a place the table does
    not know, the names are compared as text.
    """
    if unit == "location" and place is not None and a and b:
        here = _known(place, a)
        there = _known(place, b)
        if here is not None and there is not None:
            return here.name == there.name and (
                _squash(a.get("venue_field")) == _squash(b.get("venue_field"))
            )
    return same(unit, a, b)


def _known(place, value: dict):
    for name in (value.get("venue_name"), value.get("venue_raw")):
        if name:
            found = place(name)
            if found is not None:
                return found
    return None


def judge(unit: str, *, value: dict, baseline: dict, upstream: dict, place=None) -> str:
    """Has the source caught up with a fix, stayed put, or moved somewhere else?

    In that order, which matters when the baseline and the fix agree — they
    cannot, since a fix equal to its source is never stored, but a fix the
    source has reached is lifted rather than kept, whatever the baseline says.
    """
    if matches(unit, upstream, value, place=place):
        return CONVERGED
    if matches(unit, upstream, baseline, place=place):
        return UNCHANGED
    return CHANGED
