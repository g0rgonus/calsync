"""iCalendar serialization, plus a directory-backed target.

The serializer here is shared with the CalDAV target — CalDAV is this format
over HTTP. Keeping it a pure function means the interesting parts (structured
location, alarms, tombstones) are testable without a server.

Writing to a directory is also a genuinely useful target on its own: it makes
`--dry-run` real output rather than a print statement, and the result can be
committed to git for a free history.
"""

from __future__ import annotations

from pathlib import Path

from icalendar import Alarm, Calendar, Event as VEvent

from ..render import RenderedEvent
from . import Capabilities, TargetRef, register

PRODID = "-//calsync//EN"

#: Metres. Apple uses this to decide when you've "arrived"; a field-sized
#: radius keeps travel-time alerts from firing across a large park.



def to_vevent(event: RenderedEvent, *, sequence: int = 0) -> VEvent:
    ve = VEvent()
    ve.add("uid", event.uid)
    ve.add("summary", event.title)
    if event.all_day:
        # DATE values, not timestamps: the feed published a day and no time, and
        # a client given midnight renders "Semifinal Games, 12:00 AM". DTEND is
        # exclusive on a DATE (RFC 5545), which is how the parse stored it.
        # `icalendar` emits VALUE=DATE for a `date`, so the dates go in as dates.
        ve.add("dtstart", event.starts_at.date())
        ve.add("dtend", event.ends_at.date())
    else:
        ve.add("dtstart", event.starts_at)
        ve.add("dtend", event.ends_at)
    ve.add("dtstamp", event.starts_at)
    # Our own sequence, never the upstream one: some publishers bump theirs
    # when an event merely ends, which would notify subscribers about games
    # that already happened.
    ve.add("sequence", sequence)

    if event.body:
        ve.add("description", event.body)
    if event.location_text:
        ve.add("location", event.location_text)
    if event.url:
        ve.add("url", event.url)

    ve.add("status", "CANCELLED" if event.cancelled else "CONFIRMED")

    for key, value in event.provenance.items():
        ve.add(f"X-CALSYNC-{key.upper()}", value)

    if event.alarm_minutes:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", event.title)
        alarm.add("trigger", -_minutes(event.alarm_minutes))
        ve.add_component(alarm)

    return ve


def _minutes(value: int):
    from datetime import timedelta

    return timedelta(minutes=value)


def to_ics(event: RenderedEvent, *, sequence: int = 0) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add_component(to_vevent(event, sequence=sequence))
    return cal.to_ical()


@register("ics_file")
class IcsFileTarget:
    """Write one `.ics` per event under ``directory/<collection>/``."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            custom_properties=True,
            alarms=True,
            cancellation_tombstones=True,
            creates_collections=True,
        )

    def _path(self, collection: str, uid: str) -> Path:
        safe = uid.replace("/", "_")
        return self.directory / collection / f"{safe}.ics"

    def ensure_collection(self, collection: str) -> None:
        (self.directory / collection).mkdir(parents=True, exist_ok=True)

    def upsert(self, event: RenderedEvent, previous: TargetRef | None = None) -> TargetRef:
        if previous is not None and previous.collection != event.collection:
            # Collection change is a move: remove the old file, or the event
            # exists in two calendars at once.
            self._path(previous.collection, previous.remote_id).unlink(missing_ok=True)

        self.ensure_collection(event.collection)
        path = self._path(event.collection, event.uid)
        sequence = 0 if previous is None else 1
        path.write_bytes(to_ics(event, sequence=sequence))
        return TargetRef(collection=event.collection, remote_id=event.uid)

    def cancel(self, ref: TargetRef) -> None:
        self._path(ref.collection, ref.remote_id).unlink(missing_ok=True)
