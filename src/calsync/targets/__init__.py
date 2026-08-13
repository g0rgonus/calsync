"""Calendar targets — where normalized events get written.

Radicale is one option, not the design. A deployment might write straight to
Google Calendar, to iCloud over CalDAV, or to a directory of `.ics` files.

Targets receive a :class:`~calsync.render.RenderedEvent` and serialize it
themselves. That matters because the wire formats are not translations of each
other: iCalendar carries `X-APPLE-STRUCTURED-LOCATION` and arbitrary `X-`
properties, Google carries `extendedProperties.private` and geocodes a plain
location string, and neither can round-trip the other's identifiers.

Capabilities are declared rather than assumed, so the writer can degrade
knowingly instead of emitting properties a target will silently drop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..render import RenderedEvent


@dataclass(frozen=True)
class Capabilities:
    #: Apple's X-APPLE-STRUCTURED-LOCATION — an exact pin plus a friendly
    #: title. CalDAV only; Google geocodes the location string instead.
    structured_location: bool = False
    #: Somewhere to stash our own identifiers so a foreign edit can be spotted.
    custom_properties: bool = False
    #: Per-event reminders rather than calendar-wide defaults.
    alarms: bool = False
    #: A tombstone that propagates deletion, vs. a hard delete that leaves
    #: stale copies on devices that already synced.
    cancellation_tombstones: bool = False
    #: Collections can be created on demand.
    creates_collections: bool = False


@dataclass(frozen=True)
class TargetRef:
    """What a target hands back so we can update or cancel the event later."""

    collection: str
    remote_id: str
    etag: str | None = None


@runtime_checkable
class CalendarTarget(Protocol):
    kind: str

    def capabilities(self) -> Capabilities: ...

    def ensure_collection(self, collection: str) -> None: ...

    def upsert(
        self, event: RenderedEvent, previous: TargetRef | None = None
    ) -> TargetRef: ...

    def cancel(self, ref: TargetRef) -> None: ...


class TargetError(RuntimeError):
    """A target write failed. Never interpreted as 'the event is gone'."""


_REGISTRY: dict[str, type] = {}


def register(kind: str):
    def wrap(cls):
        _REGISTRY[kind] = cls
        cls.kind = kind
        return cls

    return wrap


def available() -> list[str]:
    return sorted(_REGISTRY)


def build(kind: str, **config):
    """Instantiate a target by its configured ``kind``."""
    try:
        cls = _REGISTRY[kind]
    except KeyError:
        raise TargetError(
            f"unknown target kind {kind!r}; available: {', '.join(available()) or 'none'}"
        ) from None
    return cls(**config)


def move_required(previous: TargetRef | None, event: RenderedEvent) -> bool:
    """Has the event changed collections?

    In CalDAV a collection is a distinct URL, so this is a delete-then-create,
    not an update. Treating it as an update leaves a ghost copy behind in the
    old collection — the duplicate-in-a-shared-calendar failure this system
    exists to avoid.
    """
    return previous is not None and previous.collection != event.collection


from . import caldav, google, ics_file  # noqa: E402,F401  (populate the registry)
