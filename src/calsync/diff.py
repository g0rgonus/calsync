"""Diff a poll against known state, with the mass-disappearance guard.

Player360 has no STATUS field: a cancelled event simply vanishes from the feed
(docs/sources/player360.md, trap 2). Disappearance is therefore the only
cancellation signal available — and a truncated or wrong-scope 200 response
looks exactly like a cancelled season.

So the guard is not optional polish. It is the thing standing between a bad
fetch and a wiped family calendar.
"""

from __future__ import annotations

from collections.abc import Callable
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

    #: Set when a guard trips. Affected operations are withheld and must be
    #: confirmed by a human before anything reaches the calendar.
    anomaly: str | None = None

    #: Which guard tripped: "disappearance" or "identity". They withhold
    #: different things, so the caller has to be able to tell them apart.
    anomaly_kind: str | None = None

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
    counts_as_evidence: Callable[[str], bool] | None = None,
) -> Diff:
    """Compare a poll against ``{uid: content_hash}`` of what we already have.

    Only *future* known events count toward disappearance: past events aging
    out of the feed's rolling window is normal and must never look like a
    cancellation.

    ``counts_as_evidence`` decides which uids the guard's arithmetic is measured
    over; everything counts by default. It exists for events calsync *derived*
    rather than read — a warm-up (`warmup.py`) is a deterministic function of
    its game, so it appears and vanishes exactly when the game does and carries
    no independent evidence about whether this fetch can be trusted. Counting it
    would double every disappearance in a games-only feed, tripping at two real
    cancellations a threshold measured against four, and would make switching
    the feature off — a whole season of synthetic events going at once — look
    like the catastrophe this guard exists to refuse.

    Only the *counting* is filtered. Withholding is not: a tripped guard still
    holds every cancellation in the poll, which is what keeps a game and its
    warm-up from being resolved differently.
    """
    counts = counts_as_evidence or (lambda uid: True)
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

    # Total identity turnover: nothing we knew is present, and nothing present
    # is anything we knew. A real season never rolls over this cleanly — this is
    # the signature of a feed whose UIDs aren't stable (one observed source
    # embeds a generation timestamp, so every poll mints fresh UIDs for the same
    # events).
    #
    # Unlike a disappearance, BOTH halves are withheld. Applying the creations
    # would duplicate the entire season, which is the failure the disappearance
    # guard alone does not catch.
    if known and incoming and len(missing) == len(known) and not result.updated \
            and not result.unchanged:
        result.anomaly = (
            f"none of {len(known)} tracked events matched any of {len(incoming)} "
            f"incoming events — the feed's UIDs look unstable; holding everything "
            f"pending confirmation"
        )
        result.anomaly_kind = "identity"
        result.created = []
        return result

    if not missing:
        return result

    tracked = sum(1 for uid in known if counts(uid))
    vanished = [uid for uid in missing if counts(uid)]
    over_count = len(vanished) > max_count
    over_pct = tracked > 0 and (len(vanished) / tracked) > max_pct

    if over_count or over_pct:
        pct = (len(vanished) / tracked * 100) if tracked else 0
        result.anomaly = (
            f"{len(vanished)} of {tracked} tracked events ({pct:.0f}%) vanished "
            f"from the feed in one poll — holding all cancellations pending "
            f"confirmation"
        )

        result.anomaly_kind = "disappearance"
        return result

    result.cancelled = missing
    return result
