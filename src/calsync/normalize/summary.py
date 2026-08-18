"""Turn an upstream SUMMARY into structured detail.

Two jobs, in order:

1. Parse a league match to recover the opponent. Player360 embeds it:
   "U10PL PSL Match vs Harbour FC U10".
2. Otherwise strip tokens that identify *our own* team and use the remainder.
   "U10PL Practice" -> "Practice".

Deterministic by design. No model is involved, so the same feed always renders
the same title, and a title change is always traceable to a config change.
"""

from __future__ import annotations

import re

# "U10PL PSL Match vs Harbour FC U10"
#   team ─┘   league ┘ type ┘     └─ opponent
LEAGUE_MATCH = re.compile(
    r"^(?P<team>\S+)\s+(?P<league>\S+)\s+Match\s+vs\.?\s+(?P<opponent>.+?)\s*$",
    re.IGNORECASE,
)

_WS = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip(" -–—·,")


def strip_known_tokens(summary: str, tokens: tuple[str, ...]) -> str:
    """Remove strings that identify our own team.

    Tokens must arrive longest-first so "Vanguard Academy" is consumed before
    "Vanguard" can leave a dangling "Academy". Matching is whole-word and
    case-insensitive; a token that would empty the string is skipped, since a
    bare "" is worse than a redundant label.
    """
    out = summary
    for token in tokens:
        if not token:
            continue
        candidate = re.sub(
            rf"(?<!\w){re.escape(token)}(?!\w)", " ", out, flags=re.IGNORECASE
        )
        if _collapse(candidate):
            out = candidate
    return _collapse(out)


def strip_age_suffix(opponent: str, age_group: str | None) -> str:
    """Drop a trailing age band only when it matches ours.

    "Harbour FC U10" -> "Harbour FC" for a U10 side. A U11 opponent keeps its
    suffix, because playing up or down a band is worth seeing.
    """
    if not age_group:
        return opponent
    trimmed = re.sub(rf"\s*(?<!\w){re.escape(age_group)}(?!\w)\s*$", "", opponent,
                     flags=re.IGNORECASE)
    return _collapse(trimmed) or opponent


def parse(summary: str, *, tokens: tuple[str, ...] = (),
          age_group: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(opponent, detail)``.

    Exactly one of the two is meaningful per event: a league match yields an
    opponent and no detail, everything else yields a detail and no opponent.
    """
    summary = _collapse(summary or "")
    if not summary:
        return None, None

    match = LEAGUE_MATCH.match(summary)
    if match:
        opponent = strip_age_suffix(_collapse(match.group("opponent")), age_group)
        return (opponent or None), None

    return None, (strip_known_tokens(summary, tokens) or None)
