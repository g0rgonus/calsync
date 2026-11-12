"""Decide which CalDAV collection an event belongs to.

Splitting by event type is one household's choice, not a law. The template
makes the others reachable without touching code:

    "{type}"            -> games / practices          (type-split)
    "{child}"           -> luke / leia            (one per kid)
    "{child}-{type}"    -> luke-games / luke-practices
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

#: A category's destination that keeps its events off every calendar.
SKIP = "skip"


def slugify(value: str) -> str:
    return _SLUG.sub("-", value.strip().casefold()).strip("-") or "unsorted"


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


def destination(event: Event, config: dict | None) -> str | None:
    """Where a person sent this event's category, or the source's default.

    None for a source not routed by category: it has no ``default_calendar``.
    """
    config = config or {}
    default = config.get("default_calendar")
    if not default or event.source_category is None:
        return None
    wanted = _key(event.source_category)
    for label, chosen in (config.get("categories") or {}).items():
        if _key(label) == wanted and chosen:
            return chosen
    return default


def is_new(label: str, config: dict | None) -> bool:
    """A category nobody has set a destination for yet."""
    wanted = _key(label)
    return not any(_key(k) == wanted for k in ((config or {}).get("categories") or {}))


def skipped(events, config: dict | None) -> set[str]:
    """The uids whose category a person chose to keep off every calendar."""
    return {e.uid for e in events if destination(e, config) == SKIP}


def collection_for(
    event: Event,
    activity: Activity,
    child: Child,
    settings: Settings,
    *,
    override: str | None = None,
    config: dict | None = None,
) -> str:
    """Which collection this event belongs in.

    ``override`` sends every event from one source to a single collection —
    the onboarding calendar. It is per-source rather
    than a settings change so one new feed can be staged while the rest keep
    writing to the real calendars.

    Promotion is just clearing it: the collection changes, and a changed
    collection is a move rather than an update, so the events relocate on the
    next sync without duplicating.
    """
    if override:
        return slugify(override)

    # ``config`` is the source's. A skipped category is dropped on the way to
    # the write (`sync.py`), so it never reaches a calendar from here.
    chosen = destination(event, config)
    if chosen is not None and chosen != SKIP:
        return slugify(chosen)

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

    return templated(event.is_game, activity, child, settings)


def templated(is_game: bool, activity: Activity, child: Child, settings: Settings) -> str:
    """The collection ``collection_template`` gives a game or a non-game."""
    type_label = (
        settings.collection_game_label if is_game else settings.collection_practice_label
    )
    rendered = settings.collection_template.format(
        type=type_label,
        child=child.name,
        sport=activity.sport,
        activity=activity.name,
    )
    return slugify(rendered)
