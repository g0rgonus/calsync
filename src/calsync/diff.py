"""Diff a poll against known state, with the mass-disappearance guard.

Player360 has no STATUS field: a cancelled event simply vanishes from the feed
(docs/sources/player360.md, trap 2). Disappearance is therefore the only
cancellation signal available — and a truncated or wrong-scope 200 response
looks exactly like a cancelled season.

So the guard is not optional polish. It is the thing standing between a bad
fetch and a wiped family calendar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import Event

#: Fractions/counts above which a batch of disappearances is treated as a
#: fetch anomaly rather than a set of cancellations.
MAX_DISAPPEARANCE_PCT = 0.20
MAX_DISAPPEARANCE_COUNT = 3


@dataclass
class Diff:
    created: list[Event] = field(default_factory=list)
    updated: list[Event] = field(default_factory=list)
    unchanged: list[Event] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    #: Set when the disappearance guard trips. Cancellations are withheld and
    #: must be confirmed by a human before anything is deleted.
    anomaly: str | None = None

    @property
    def is_anomalous(self) -> bool:
        return self.anomaly is not None

    def summary(self) -> str:
        parts = [
            f"{len(self.created)} new",
            f"{len(self.updated)} changed",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.anomaly:
            parts.append(f"HELD: {self.anomaly}")
        else:
            parts.append(f"{len(self.cancelled)} cancelled")
        return ", ".join(parts)


def diff_poll(
    incoming: list[Event],
    known: dict[str, str],
    *,
    now: datetime,
    max_pct: float = MAX_DISAPPEARANCE_PCT,
    max_count: int = MAX_DISAPPEARANCE_COUNT,
) -> Diff:
    """Compare a poll against ``{uid: content_hash}`` of what we already have.

    Only *future* known events count toward disappearance: past events aging
    out of the feed's rolling window is normal and must never look like a
    cancellation.
    """
    result = Diff()
    incoming_by_uid = {e.uid: e for e in incoming}

    for event in incoming:
        previous = known.get(event.uid)
        if previous is None:
            result.created.append(event)
        elif previous != event.content_hash:
            result.updated.append(event)
        else:
            result.unchanged.append(event)

    missing = [uid for uid in known if uid not in incoming_by_uid]
    if not missing:
        return result

    tracked = len(known)
    over_count = len(missing) > max_count
    over_pct = tracked > 0 and (len(missing) / tracked) > max_pct

    if over_count or over_pct:
        pct = (len(missing) / tracked * 100) if tracked else 0
        result.anomaly = (
            f"{len(missing)} of {tracked} tracked events ({pct:.0f}%) vanished "
            f"from the feed in one poll — holding all cancellations pending "
            f"confirmation"
        )
        return result

    result.cancelled = missing
    return result
