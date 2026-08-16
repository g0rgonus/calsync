"""Decide which CalDAV collection an event belongs to.

Splitting by event type is one household's choice, not a law. The template
makes the others reachable without touching code:

    "{type}"            -> games / practices          (type-split)
    "{child}"           -> james / patrick            (one per kid)
    "{child}-{type}"    -> james-games / james-practices
    "calendar"          -> everything in one place

Reclassification between collections is a delete-then-create in CalDAV, not an
update — collections are distinct URLs. The writer must treat a changed
collection as a move, or a stale copy is left behind.
"""

from __future__ import annotations

import re

from .models import Activity, Child, Event
from .settings import Settings

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SLUG.sub("-", value.strip().casefold()).strip("-") or "unsorted"


def collection_for(
    event: Event,
    activity: Activity,
    child: Child,
    settings: Settings,
    *,
    override: str | None = None,
) -> str:
    """Which collection this event belongs in.

    ``override`` sends every event from one source to a single collection —
    the onboarding calendar (docs/ONBOARDING.md §4). It is per-source rather
    than a settings change so one new feed can be staged while the rest keep
    writing to the real calendars.

    Promotion is just clearing it: the collection changes, and a changed
    collection is a move rather than an update, so the events relocate on the
    next sync without duplicating.
    """
    if override:
        return slugify(override)

    type_label = (
        settings.collection_game_label if event.is_game else settings.collection_practice_label
    )
    rendered = settings.collection_template.format(
        type=type_label,
        child=child.name,
        sport=activity.sport,
        activity=activity.name,
    )
    return slugify(rendered)
