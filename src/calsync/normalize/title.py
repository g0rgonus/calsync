"""Render the display title.

    SUMMARY := kids " " emoji [" " detail]

The title is a *render*, not data — see docs/NAMING.md. Nothing downstream
parses it back, so the convention can change and every event can be re-rendered
without re-fetching anything.

The venue is deliberately absent: it has its own geocoded field, and Apple
Calendar shows it under the title anyway.
"""

from __future__ import annotations

from ..models import Activity, Child, Event

ALL_KIDS = "Kids"


def render_kids(children: list[Child]) -> str:
    """One kid -> full name. Two -> initials. Three or more -> "Kids".

    Initials for the two-kid form because "Patrick+James" is 13 characters
    before the emoji even starts, and week view would truncate away the emoji
    — which is the field doing the disambiguation work.
    """
    if not children:
        raise ValueError("an event needs at least one child")

    ordered = sorted(children, key=lambda c: (c.birth_order, c.name))
    if len(ordered) == 1:
        return ordered[0].name
    if len(ordered) == 2:
        return "+".join(c.initial for c in ordered)
    return ALL_KIDS


def render_detail(event: Event, activity: Activity) -> str | None:
    """Opponent for games, upstream label otherwise.

    Home/away only marks "@" when we positively know it's away. Player360's
    SUMMARY always reads "vs", so an unknown must not become "@".
    """
    if event.opponent:
        prefix = "@" if event.home is False else "vs"
        return f"{prefix} {event.opponent}"
    return event.detail or None


def render(event: Event, activity: Activity, children: list[Child]) -> str:
    parts = [render_kids(children), activity.emoji]
    detail = render_detail(event, activity)
    if detail:
        parts.append(detail)
    return " ".join(p for p in parts if p)
