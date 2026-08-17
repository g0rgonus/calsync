"""Spotting a season that has ended.

Rec teams are recreated every season under a new name with a new feed id, so a
source does not wind down — it dies, and its feed starts returning 404 forever.
Backing off (``polling.py``) stops that costing anything, but it also makes the
dead source *quiet*, and a quiet dead source sits in the list looking like a
working one until somebody notices the event count never changes.

**This only ever labels. It never acts.** Whether a source that stopped
answering is a finished season or a host having a bad fortnight is not something
calsync can know, and the two are indistinguishable from here — so nothing is
retired, disabled, cancelled or deleted on the strength of it. It puts the
question in front of the operator, next to the button that answers it, and that
is the whole of its job.

The verdict is derived on read rather than stored. A stored flag would need
maintaining, could contradict the data it was computed from, and would go stale
the moment a feed came back; a query cannot. Nothing here has a schema change
behind it.

The three conditions are required together, because each alone has an innocent
explanation:

- **sustained failure** alone is a host outage,
- **nothing upcoming** alone is the gap between seasons on a healthy feed,
- **a long time since success** alone is a quiet winter.

All three at once is what the end of a season actually looks like.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: Consecutive failed polls before a feed counts as sustained failure. With the
#: backoff in `polling.py` this is a few days of trying, not a few minutes.
MIN_CONSECUTIVE_ERRORS = 5

#: And it has to have been failing for a while in wall-clock terms too — five
#: fast failures during one bad afternoon is not a season ending.
MIN_DAYS_SINCE_SUCCESS = 14


@dataclass(frozen=True)
class Verdict:
    source_id: str
    suspected: bool
    consecutive_errors: int = 0
    days_since_success: int | None = None
    upcoming_events: int = 0
    #: Plain English, for a page. Empty when nothing is suspected.
    reason: str = ""

    @property
    def headline(self) -> str:
        return "This season looks finished." if self.suspected else ""


def assess(
    *,
    source_id: str,
    consecutive_errors: int,
    last_success_at: datetime | None,
    upcoming_events: int,
    now: datetime,
    min_errors: int = MIN_CONSECUTIVE_ERRORS,
    min_days: int = MIN_DAYS_SINCE_SUCCESS,
) -> Verdict:
    """Does this look like a season that ended? Pure, so it can be argued with."""
    days = None
    if last_success_at is not None:
        days = max((now - last_success_at).days, 0)

    failing = consecutive_errors >= min_errors
    stale = days is None or days >= min_days
    empty = upcoming_events == 0

    verdict = Verdict(
        source_id=source_id,
        suspected=failing and stale and empty,
        consecutive_errors=consecutive_errors,
        days_since_success=days,
        upcoming_events=upcoming_events,
    )
    if not verdict.suspected:
        return verdict

    since = (
        f"nothing has worked for {days} days"
        if days is not None
        else "it has never polled successfully"
    )
    return Verdict(
        **{
            **verdict.__dict__,
            "reason": (
                f"{consecutive_errors} failed polls in a row, {since}, and no "
                "events left on the calendar. That is what the end of a season "
                "looks like — but a feed that is merely broken looks the same, "
                "so nothing has been changed."
            ),
        }
    )


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def consecutive_errors(conn, source_id: str, *, limit: int = 50) -> int:
    """Failed polls since the last one that worked.

    Counted from `poll_runs` rather than kept as a counter, so it is always the
    truth about what actually happened and survives a restart — unlike the
    in-memory backoff, which forgives on restart because retrying is its job.
    """
    total = 0
    for run in conn.execute(
        "SELECT status FROM poll_runs WHERE source_id = ? ORDER BY id DESC LIMIT ?",
        (source_id, limit),
    ):
        if run["status"] != "error":
            break
        total += 1
    return total


def upcoming_events(conn, source_id: str, *, now: datetime) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM event_state "
            " WHERE source_id = ? AND cancelled = 0 AND starts_at >= ?",
            (source_id, now.isoformat()),
        ).fetchone()["n"]
    )


def for_source(conn, source_id: str, *, now: datetime | None = None) -> Verdict:
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT last_success_at FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    return assess(
        source_id=source_id,
        consecutive_errors=consecutive_errors(conn, source_id),
        last_success_at=_parse(row["last_success_at"] if row else None),
        upcoming_events=upcoming_events(conn, source_id, now=now),
        now=now,
    )


__all__ = ["Verdict", "assess", "for_source", "MIN_CONSECUTIVE_ERRORS",
           "MIN_DAYS_SINCE_SUCCESS", "timedelta"]
