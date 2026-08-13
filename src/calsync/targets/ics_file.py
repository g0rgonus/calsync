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
from icalendar.prop import vUri

from ..render import RenderedEvent
from . import Capabilities, TargetRef, register

PRODID = "-//calsync//EN"

#: Metres. Apple uses this to decide when you've "arrived"; a field-sized
#: radius keeps travel-time alerts from firing across a large park.
APPLE_RADIUS = 72


def _apple_structured_location(event: RenderedEvent) -> vUri:
    """Apple's exact-pin property, as Apple's own clients write it.

    Must be a URI value with real parameters — encoding it as text escapes the
    semicolons and commas, and Apple then ignores the whole property. This is
    the difference between "navigate to the park" and "navigate to the right
    corner of the park", which for youth sports is most of the value.
    """
    prop = vUri(f"geo:{event.lat:.6f},{event.lon:.6f}")
    prop.params["VALUE"] = "URI"
    prop.params["X-TITLE"] = event.venue_name or event.location_text or ""
    if event.location_text:
        prop.params["X-ADDRESS"] = event.location_text
    prop.params["X-APPLE-RADIUS"] = str(APPLE_RADIUS)
    return prop


def to_vevent(event: RenderedEvent, *, sequence: int = 0) -> VEvent:
    ve = VEvent()
    ve.add("uid", event.uid)
    ve.add("summary", event.title)
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

    if event.has_coordinates:
        ve.add("geo", (event.lat, event.lon))
        # encode=0: the value is already a vUri with its parameters set, and
        # letting icalendar re-encode it would escape it into uselessness.
        ve.add("X-APPLE-STRUCTURED-LOCATION", _apple_structured_location(event), encode=0)

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
            structured_location=True,
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
