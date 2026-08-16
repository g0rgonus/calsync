"""Turn a normalized Event into everything a calendar target needs.

Deliberately produces a domain object, **not** an ICS blob. A CalDAV target
serializes it to VEVENT; a Google target serializes it to JSON. If this
returned iCalendar text, every non-CalDAV target would have to parse it back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import Activity, Child, Event
from .normalize import title as title_norm
from .routing import collection_for
from .settings import Settings


@dataclass(frozen=True)
class RenderedEvent:
    uid: str
    collection: str
    title: str
    starts_at: datetime
    ends_at: datetime
    tz: str
    body: str
    location_text: str | None = None
    venue_name: str | None = None
    lat: float | None = None
    lon: float | None = None
    url: str | None = None
    alarm_minutes: int | None = None
    is_game: bool = False
    cancelled: bool = False
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lon is not None


def build_body(
    event: Event, activity: Activity, settings: Settings, *, manage_url: str | None = None
) -> str:
    """Compose the event body.

    Lines appear only when populated, so a missing kit or arrival time never
    leaves a dangling label.

    Venue-local time is stated unconditionally. Calendar clients render in the
    *device's* timezone, so anyone reading this from another zone otherwise
    sees a time that is not the start time — quietly misleading rather than
    obviously wrong.
    """
    lines: list[str] = []

    descriptor = activity.name
    if activity.official_name and activity.official_name != activity.name:
        descriptor = f"{activity.name} ({activity.official_name})"
    lines.append(f"{activity.sport.replace('_', ' ').title()} · {descriptor}")

    local = event.local_start
    label = "Start" if not event.is_game else "Start"
    lines.append(f"{label} {local:%H:%M} {local:%Z}")

    if event.body:
        lines.append(event.body)
    if event.kit:
        lines.append(f"Kit: {event.kit}")
    if event.arrive_at:
        lines.append(f"Arrive: {event.arrive_at.astimezone(local.tzinfo):%H:%M}")
    if event.venue and event.venue.field:
        # Which field, kept out of LOCATION so the map pin stays navigable.
        lines.append(f"Field: {event.venue.field}")
    if event.venue and event.venue.address:
        lines.append(event.venue.address)
    if event.source_id:
        lines.append(f"Source: {event.source_id}")
    if manage_url:
        lines.append(f"Manage: {manage_url}")

    return "\n".join(lines)


def render(
    event: Event,
    activity: Activity,
    children: list[Child],
    settings: Settings,
    *,
    alarm_minutes: int | None = None,
    manage_url: str | None = None,
    cancelled: bool = False,
) -> RenderedEvent:
    primary = min(children, key=lambda c: (c.birth_order, c.name))
    venue = event.venue

    location_text = None
    if venue:
        if venue.name and venue.address:
            location_text = f"{venue.name}, {venue.address}"
        else:
            location_text = venue.name or venue.raw

    return RenderedEvent(
        uid=event.uid,
        collection=collection_for(event, activity, primary, settings),
        title=title_norm.render(event, activity, children, settings),
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        tz=event.tz,
        body=build_body(event, activity, settings, manage_url=manage_url),
        location_text=location_text,
        venue_name=venue.name if venue else None,
        lat=venue.lat if venue else None,
        lon=venue.lon if venue else None,
        url=event.url,
        alarm_minutes=alarm_minutes,
        is_game=event.is_game,
        cancelled=cancelled,
        provenance={
            k: v
            for k, v in {
                "uid": event.uid,
                "source": event.source_id or "",
                "activity": activity.id,
                "hash": event.content_hash or "",
            }.items()
            if v
        },
    )
