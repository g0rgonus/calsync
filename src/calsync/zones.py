"""Which timezone names the console offers, and which it will accept.

This was a free-text box, and free text is how a deployment stores `EDT`:
`ZoneInfo` cannot load it, so every local time falls back to UTC. Names that do
load can still be wrong — a fixed-offset zone like `EST` has no daylight-saving
rules, and reads an hour out for half the year with nothing reporting it.

So the list is city zones, where the tz database keeps the switch dates. Any
loadable name is still accepted, because `EST5EDT` does handle its switches.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

#: Used only when the tz database is missing, which a slim base image makes
#: possible. Short on purpose: the console renders a text box instead of a list
#: when the offer is this small, rather than showing a picker with holes in it.
MINIMAL: tuple[str, ...] = (
    "UTC",
    "America/Chicago", "America/Denver", "America/Halifax",
    "America/Los_Angeles", "America/New_York", "America/Toronto",
    "America/Vancouver",
    "Europe/Dublin", "Europe/London", "Europe/Paris",
)


@lru_cache(maxsize=1)
def offered() -> tuple[str, ...]:
    """Every city zone this machine knows, `UTC` first.

    Cached because `available_timezones()` walks the whole database and two
    forms read this on every render. `Etc/` is dropped with the abbreviations:
    it is fixed-offset too, and signed the opposite way round from every other
    notation.
    """
    try:
        names = available_timezones()
    except Exception:  # noqa: BLE001 — a missing tz database is not a crash
        return MINIMAL
    cities = sorted(n for n in names if "/" in n and not n.startswith("Etc/"))
    return ("UTC", *cities) if cities else MINIMAL


def loadable(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def choices(current: str | None = None) -> tuple[str, ...]:
    """The offer, with ``current`` on it whether or not it qualifies.

    A stored `EST5EDT` has to stay selected when the form renders, or opening
    the page and pressing save moves the household to whatever sorts first.
    """
    names = offered()
    if current and current not in names:
        return (current, *names)
    return names
