"""Diff a poll against known state, with the mass-disappearance guard.

Two kinds of cancellation reach this module, and they are trusted differently.

An event the feed marks ``STATUS:CANCELLED`` (``called_off``) is cancelled
outright. It is a positive statement inside a feed that parsed, about an event
that is still there — nothing a broken fetch produces looks like it — so no
guard applies and no person is asked, whether it is one game or a rained-out
weekend of ten.

An event that simply vanishes is the other kind, and the only kind most feeds
offer. A truncated or wrong-scope 200 response looks exactly like a cancelled
season, so the guard is not optional polish: it is the thing standing between a
bad fetch and a wiped family calendar.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
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
    #: Tracked events the feed itself marked cancelled. Never held: a guard
    #: protects against absences, and these are not absent.
    called_off: list[str] = field(default_factory=list)

    #: Set when a guard trips. Affected operations are withheld and must be
    #: confirmed by a human before anything reaches the calendar.
    anomaly: str | None = None

    #: Which guard tripped: "disappearance" or "identity". They withhold
    #: different things, so the caller has to be able to tell them apart.
    anomaly_kind: str | None = None

    #: What a tripped disappearance guard withheld: every tracked uid missing
    #: from this poll, warm-ups included. Kept rather than discarded so a person
    #: can see the exact set and confirm it (`confirm`) — a guard that says
    #: "pending confirmation" and offers no way to confirm holds forever, and a
    #: source stuck that way also holds every *real* cancellation behind it.
    held_cancellations: list[str] = field(default_factory=list)

    @property
    def is_anomalous(self) -> bool:
        return self.anomaly is not None

    def confirm(self, expected: str | None) -> bool:
        """Release held cancellations a person has seen and agreed to.

        ``expected`` is the `fingerprint` of the set that person was shown. It
        must match this poll's set exactly: the feed is fetched again between
        the page and the button, and a confirmation is an answer about *those*
        events, not licence to cancel whatever is missing by the time it lands.
        Only the disappearance guard can be confirmed — the identity guard
        withholds creations too, and confirming it would duplicate a season.
        """
        if (expected is None or self.anomaly_kind != "disappearance"
                or fingerprint(self.held_cancellations) != expected):
            return False
        self.cancelled = self.held_cancellations
        self.held_cancellations = []
        self.anomaly = None
        self.anomaly_kind = None
        return True


def fingerprint(uids: Iterable[str]) -> str:
    """A short, order-independent name for a set of uids."""
    digest = hashlib.sha256("\n".join(sorted(uids)).encode()).hexdigest()
    return digest[:16]


def diff_poll(
    incoming: list[Event],
    known: dict[str, str],
    *,
    now: datetime,
    max_pct: float = MAX_DISAPPEARANCE_PCT,
    max_count: int = MAX_DISAPPEARANCE_COUNT,
    counts_as_evidence: Callable[[str], bool] | None = None,
    called_off: Iterable[str] = (),
) -> Diff:
    """Compare a poll against ``{uid: content_hash}`` of what we already have.

    ``known`` must already be bounded by the same sync window the incoming
    events were filtered by (`repo.known_hashes`' ``since``), which is what
    stops an event ageing out of the window looking like a cancellation. Within
    that window, past events count toward the guard as much as future ones —
    ``now`` is not consulted here.

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

    ``called_off`` is every uid the feed marked cancelled — warm-ups of those
    games included, which the caller derives. They are not in ``incoming``, and
    they are not missing either: the tracked ones go to `Diff.called_off`
    whatever any guard decides, and are left out of the guard's arithmetic on
    both sides. Counting them as tracked would dilute the percentage; counting
    them as vanished would hold a weekend of real cancellations behind the very
    check that exists to tell a cancellation from a fault.
    """
    counts = counts_as_evidence or (lambda uid: True)
    result = Diff()
    incoming_by_uid = {e.uid: e for e in incoming}
    announced = set(called_off) - set(incoming_by_uid)
    result.called_off = [uid for uid in known if uid in announced]

    for event in incoming:
        previous = known.get(event.uid)
        if previous is None:
            result.created.append(event)
        elif previous != event.content_hash:
            result.updated.append(event)
        else:
            result.unchanged.append(event)

    missing = [
        uid for uid in known if uid not in incoming_by_uid and uid not in announced
    ]

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

    tracked = sum(1 for uid in known if counts(uid) and uid not in announced)
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
        result.held_cancellations = missing
        return result

    result.cancelled = missing
    return result
