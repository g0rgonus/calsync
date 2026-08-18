"""Decide which CalDAV collection an event belongs to.

Splitting by event type is one household's choice, not a law. The template
makes the others reachable without touching code:

    "{type}"            -> games / practices          (type-split)
    "{child}"           -> jesse / parker            (one per kid)
    "{child}-{type}"    -> jesse-games / jesse-practices
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

    # Before the type split, because an event we cannot classify has no honest
    # answer to "game or practice" — that is the whole question. Held here it is
    # visible, tracked in `event_state`, and reachable in a calendar client,
    # rather than filed under a guess. Releasing it is a collection change,
    # which the sync loop already treats as a move.
    #
    # Deliberately *after* the staging override: a source still being onboarded
    # is already held somewhere, and splitting its events across two holding
    # calendars would make the promotion gate harder to read, not safer.
    if event.needs_enrichment and settings.enrichment_collection:
        return slugify(settings.enrichment_collection)

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
