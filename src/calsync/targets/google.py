"""Google Calendar target.

Google is not CalDAV wearing a different hat, and the differences are exactly
why targets serialize the domain object themselves:

- **Event ids are constrained.** Google requires base32hex — lowercase ``a-v``
  and ``0-9``, 5 to 1024 characters. A UID like ``360Player-event-4716716`` is
  rejected outright, so ids are derived deterministically instead.
- **No arbitrary properties.** ``extendedProperties.private`` takes their
  place, and it is where our real UID lives so an event can be matched back.
- **Location is a plain string** that Google geocodes itself — which is now
  what every target does, so this stopped being a difference.
- **Collections are calendar ids**, not paths, so a logical collection name
  has to be mapped to something like ``…@group.calendar.google.com``.

The payload builder is a pure function and fully tested. The transport is a
thin injectable seam, so this stays verifiable without credentials.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Callable

from ..render import RenderedEvent
from . import Capabilities, TargetError, TargetRef, register

#: Google's allowed event-id alphabet (base32hex).
_B32HEX = "0123456789abcdefghijklmnopqrstuv"


def google_event_id(uid: str) -> str:
    """Derive a Google-legal, stable id from our UID.

    Deterministic so the same event maps to the same id on every run — a
    random id would create a duplicate on each poll.
    """
    digest = hashlib.sha256(uid.encode()).digest()[:15]
    value = int.from_bytes(digest, "big")
    out = []
    while value:
        value, rem = divmod(value, 32)
        out.append(_B32HEX[rem])
    encoded = "".join(reversed(out)) or "0"
    return encoded.rjust(24, "0")


def to_google_event(event: RenderedEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": google_event_id(event.uid),
        "summary": event.title,
        "start": {"dateTime": event.starts_at.isoformat(), "timeZone": event.tz},
        "end": {"dateTime": event.ends_at.isoformat(), "timeZone": event.tz},
        "status": "cancelled" if event.cancelled else "confirmed",
        # Google has no X- properties; this is the only place our identity
        # survives a round trip.
        "extendedProperties": {
            "private": {f"calsync_{k}": v for k, v in event.provenance.items()}
        },
    }

    if event.body:
        payload["description"] = event.body
    if event.location_text:
        payload["location"] = event.location_text
    if event.url:
        payload["source"] = {"title": "calsync", "url": event.url}

    if event.alarm_minutes:
        payload["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": event.alarm_minutes}],
        }

    return payload


@register("google")
class GoogleCalendarTarget:
    """Write to Google Calendar.

    ``calendar_map`` binds logical collection names to Google calendar ids::

        {"games": "abc...@group.calendar.google.com", "practices": "def..."}

    An unmapped collection is an error rather than a guess: silently writing a
    kid's schedule into the wrong calendar is worse than failing the poll.
    """

    def __init__(
        self,
        calendar_map: dict[str, str],
        transport: Callable[[str, str, dict | None], dict] | None = None,
    ):
        self.calendar_map = calendar_map
        self._transport = transport

    def capabilities(self) -> Capabilities:
        return Capabilities(
            custom_properties=True,      # extendedProperties.private
            alarms=True,
            cancellation_tombstones=True,
            creates_collections=False,   # calendars are created out of band
        )

    def calendar_id(self, collection: str) -> str:
        try:
            return self.calendar_map[collection]
        except KeyError:
            raise TargetError(
                f"no Google calendar mapped for collection {collection!r}; "
                f"mapped: {sorted(self.calendar_map) or 'none'}"
            ) from None

    def ensure_collection(self, collection: str) -> None:
        self.calendar_id(collection)  # validates the mapping exists

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if self._transport is None:
            raise TargetError("google target has no transport configured")
        return self._transport(method, path, body)

    def upsert(self, event: RenderedEvent, previous: TargetRef | None = None) -> TargetRef:
        cal = self.calendar_id(event.collection)
        payload = to_google_event(event)

        if previous is not None and previous.collection != event.collection:
            # Google can move an event between calendars, but only as an
            # explicit operation — an update against the new calendar would
            # create a second copy.
            self._call(
                "POST",
                f"/calendars/{self.calendar_id(previous.collection)}"
                f"/events/{previous.remote_id}/move?destination={cal}",
            )

        self._call("PUT", f"/calendars/{cal}/events/{payload['id']}", payload)
        return TargetRef(collection=event.collection, remote_id=payload["id"])

    def cancel(self, ref: TargetRef) -> None:
        cal = self.calendar_id(ref.collection)
        self._call("DELETE", f"/calendars/{cal}/events/{ref.remote_id}")
