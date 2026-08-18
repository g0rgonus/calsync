"""Split a free-text LOCATION into venue name and street address.

Player360 glues them together with inconsistent punctuation:

    Alder Reach Memorial Park 7160 Kestrel Ln, Fenwick, NX 40219
    Thistledown Park 1009 Thistledown Rd, Marbury NX 40114   <- no comma before VA

Splitting on the first street-number run matches what Apple's own data
detector does with these strings, which is a decent sign it's the right cut.
"""

from __future__ import annotations

import re

from ..models import Venue

# First run of digits that begins a token and is followed by more text —
# i.e. a street number, not a "Field 4" trailing designator.
STREET_SPLIT = re.compile(r"^(?P<name>.*?)\s+(?P<addr>\d+\s+\S.*)$", re.DOTALL)

_WS = re.compile(r"\s+")

#: A trailing field/court designator: "#2", "Field 3", "Court 1", "Gym".
#:
#: Anchored to the end and requiring a word boundary on the keyword, so
#: "Kingsmere Meadow Park Soccer Fields" keeps its name intact — "Fields" plural
#: with no number is part of what the place is called, not which field you want.
FIELD_TAIL = re.compile(
    r"\s+(?P<field>"
    r"#\s*\w+"
    r"|(?:field|fld|court|diamond|rink|pitch)\b\s*#?\s*[\w-]+"
    r"|gym(?:nasium)?\b"
    r")\s*$",
    re.IGNORECASE,
)


def split_field(text: str) -> tuple[str, str | None]:
    """Separate the venue from the field within it.

    ``"Kingsmere #2"`` -> ``("Kingsmere", "#2")``

    Venue identity is what carries an address and a pin, and every field at a
    park shares both. Folding the designator into the name would mint a separate
    venue — and a separate geocode — per field.
    """
    match = FIELD_TAIL.search(text or "")
    if match is None:
        return (text or "").strip(), None
    field = _WS.sub(" ", match.group("field")).strip()
    return text[: match.start()].strip(), field or None


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip().strip(",").strip()


def parse(location: str | None) -> Venue | None:
    """Best-effort split. Never raises, never guesses coordinates.

    An unsplittable string still yields a Venue carrying the raw text, so the
    event keeps a human-readable location even when geocoding can't run. A
    non-clickable location beats a wrong pin.
    """
    if not location or not location.strip():
        return None

    raw = _collapse(location)
    match = STREET_SPLIT.match(raw)
    if not match:
        return Venue(raw=raw, name=raw)

    name = _collapse(match.group("name"))
    address = _collapse(match.group("addr"))
    if not name or not address:
        return Venue(raw=raw, name=raw)

    return Venue(raw=raw, name=name, address=address)


def matches_home(venue: Venue | None, home_venue: str | None) -> bool | None:
    """Is this the activity's home ground?

    Returns None when it can't be determined — the caller must not collapse
    that into "away", since Player360's SUMMARY always reads "vs" regardless
    and an unknown would otherwise silently render as "@".
    """
    if venue is None or not home_venue:
        return None
    needle = home_venue.casefold()
    for candidate in (venue.name, venue.raw):
        if candidate and needle in candidate.casefold():
            return True
    return False
