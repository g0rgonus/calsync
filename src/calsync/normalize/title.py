"""Render the display title from a configurable template.

Default:  "{kids} {emoji} {detail}"  ->  "James ⚽️ vs Beach FC"

The title is a *render*, not data — see docs/NAMING.md. Nothing downstream
parses it back, so a deployment can change the convention and re-render every
event without re-fetching anything.

Available fields: {kids} {emoji} {detail} {sport} {activity} {venue}.
Empty fields collapse, so a template never leaves a dangling separator.
"""

from __future__ import annotations

import re

from ..models import Activity, Child, Event
from ..settings import Settings

_WS = re.compile(r"\s+")


def render_kids(children: list[Child], settings: Settings) -> str:
    """Collapse a set of children into a label.

    The default uses initials for the multi-kid form because full names blow
    the week-view budget: "Patrick+James" is 13 characters before the emoji
    starts, and truncation would eat the emoji — the field doing the
    disambiguation work. A deployment with shorter names can set
    ``multi_kid_style = names``.
    """
    if not children:
        raise ValueError("an event needs at least one child")

    ordered = sorted(children, key=lambda c: (c.birth_order, c.name))
    if len(ordered) == 1:
        return ordered[0].name
    if len(ordered) >= settings.all_kids_threshold:
        return settings.all_kids_label
    if settings.multi_kid_style == "names":
        return "+".join(c.name for c in ordered)
    return "+".join(c.initial for c in ordered)


def render_detail(event: Event, settings: Settings) -> str:
    """Opponent for games, upstream label otherwise.

    Away is marked only when positively known. Some feeds phrase every fixture
    as "vs" regardless of venue, so an undetermined home/away must not silently
    render as away.
    """
    if event.opponent:
        marker = settings.away_marker if event.home is False else settings.home_marker
        return f"{marker} {event.opponent}".strip()
    return event.detail or ""


def render(
    event: Event, activity: Activity, children: list[Child], settings: Settings
) -> str:
    emoji = activity.emoji or ""
    rendered = settings.title_template.format(
        kids=render_kids(children, settings),
        emoji=emoji,
        detail=render_detail(event, settings),
        sport=activity.sport,
        activity=activity.name,
        venue=(event.venue.name if event.venue and event.venue.name else ""),
    )
    return _WS.sub(" ", rendered).strip()
