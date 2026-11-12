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


#: How a rule may match a category's title. No patterns: a parent writes these
#: on a phone, and a regular expression is a support call.
MATCHES = ("starts_with", "contains")


def _rule_matches(rule: dict, label: str) -> bool:
    text, wanted = _key(rule.get("text") or ""), _key(label)
    if not text:
        return False
    if rule.get("match") == "starts_with":
        return wanted.startswith(text)
    return rule.get("match") == "contains" and text in wanted


def matching_rules(label: str, config: dict | None) -> list[int]:
    """The indexes of every rule that matches, in order; the first one wins."""
    rules = (config or {}).get("rules") or []
    return [i for i, rule in enumerate(rules) if _rule_matches(rule, label)]


def placement(label: str, config: dict | None) -> tuple[str | None, str, int | None]:
    """Where a category goes and what placed it: ``set``, ``rule`` (with its
    index) or ``default``. A category's own destination beats a rule, so one
    exception needs no rule of its own."""
    config = config or {}
    wanted = _key(label)
    for chosen_label, chosen in (config.get("categories") or {}).items():
        if _key(chosen_label) == wanted and chosen:
            return chosen, "set", None
    matched = matching_rules(label, config)
    if matched:
        return config["rules"][matched[0]]["destination"], "rule", matched[0]
    return config.get("default_calendar"), "default", None


def destination(event: Event, config: dict | None) -> str | None:
    """Where a person sent this event's category, or the source's default.

    None for a source not routed by category: it has no ``default_calendar``.
    """
    if not (config or {}).get("default_calendar") or event.source_category is None:
        return None
    return placement(event.source_category, config)[0]


def is_new(label: str, config: dict | None) -> bool:
    """A category nobody has answered: no destination of its own, no rule, and
    not kept at the default by "Keep all"."""
    config = config or {}
    if placement(label, config)[1] != "default":
        return False
    return _key(label) not in {_key(k) for k in (config.get("categories_seen") or ())}


def validate_rules(rules) -> list[dict]:
    """A rule list as stored, or ValueError naming what is wrong with it."""
    if not isinstance(rules, list):
        raise ValueError("rules are a list")
    clean = []
    for n, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            raise ValueError(f"rule {n} is not a rule")
        match = rule.get("match")
        text = " ".join(str(rule.get("text") or "").split())
        chosen = str(rule.get("destination") or "").strip()
        if match not in MATCHES:
            raise ValueError(f"rule {n} has to start with or contain its text")
        if not text:
            raise ValueError(f"rule {n} has no text to match")
        if not chosen:
            raise ValueError(f"rule {n} goes nowhere")
        clean.append({"match": match, "text": text,
                      "destination": SKIP if chosen == SKIP else slugify(chosen)})
    return clean


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

    # A held event outranks its category's calendar: the question is whether
    # it should be on any calendar at that time at all (invariant 20).
    if event.needs_enrichment and settings.enrichment_collection:
        return slugify(settings.enrichment_collection)

    # ``config`` is the source's. A skipped category is dropped on the way to
    # the write (`sync.py`), so it never reaches a calendar from here.
    chosen = destination(event, config)
    if chosen is not None and chosen != SKIP:
        return slugify(chosen)

    # The hold above comes before the type split because an event we cannot
    # classify has no honest answer to "game or practice". Held, it is visible,
    # tracked in `event_state` and reachable in a calendar client; releasing it
    # is a collection change, which the sync loop treats as a move. It comes
    # after the staging override: a source still being onboarded is already
    # held, and two holding calendars would make the gate harder to read.
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
