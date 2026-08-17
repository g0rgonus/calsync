"""Spotting a season that has ended.

Rec teams are recreated every season under a new name with a new feed id, so a
source does not wind down — the team simply stops existing. **Its feed usually
keeps working.** The team app goes on serving last spring's fixtures to anyone
who asks, indefinitely, with a 200 and a clean parse every time.

That is what makes this worth detecting and what makes the obvious detector
useless. Looking for failures finds a *broken* feed, which is a different
problem: a season that ended is perfectly healthy by every measure calsync
otherwise has. The tell is in the dates. Nothing new has been published for
months, and the newest event in the feed is a long way in the past.

**This only ever labels. It never acts.** A quiet feed and a finished season
look the same from here, and so does a team on an unusually long break — only
somebody who knows whether the club still exists can tell. So nothing is
retired, disabled, cancelled or deleted on the strength of it. It puts the
question in front of the operator, next to the button that answers it, and that
is the whole of its job.

The verdict is derived on read rather than stored. A stored flag would need
maintaining, could contradict the data it was computed from, and would go stale
the moment a feed came back; a query cannot. Nothing here has a schema change
behind it.

Two conditions, together, because either alone is innocent:

- **nothing upcoming** alone is March, before a coach has posted the fixtures,
- **an old newest event** alone cannot happen while anything is still upcoming.

Fetch failures are deliberately *not* part of the test. Requiring them was the
original mistake here: it described a feed that had gone away, which is not what
the end of a season looks like. They are reported alongside as context, because
a source that is both stale and erroring is worth knowing about, but they gate
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: How far past the newest event in a feed before the season is presumed over.
#: A rec season runs a couple of months and the gap to the next one is longer
#: than the gap between fixtures within one, so three months clears every normal
#: mid-season lull — including a summer break between a spring and autumn
#: season — while still catching a team that finished in May by August.
MIN_DAYS_SINCE_LAST_EVENT = 90


@dataclass(frozen=True)
class Verdict:
    source_id: str
    suspected: bool
    days_since_last_event: int | None = None
    upcoming_events: int = 0
    #: Context, not a condition. A stale source that is also failing is worth
    #: mentioning; a stale source that is answering happily is the normal case.
    consecutive_errors: int = 0
    #: Plain English, for a page. Empty when nothing is suspected.
    reason: str = ""

    @property
    def headline(self) -> str:
        return "This season looks finished." if self.suspected else ""


def assess(
    *,
    source_id: str,
    last_event_at: datetime | None,
    upcoming_events: int,
    now: datetime,
    consecutive_errors: int = 0,
    min_days: int = MIN_DAYS_SINCE_LAST_EVENT,
) -> Verdict:
    """Does this look like a season that ended? Pure, so it can be argued with."""
    days = None if last_event_at is None else max((now - last_event_at).days, 0)

    # `days is None` means nothing has ever been written for this source — a
    # source created five minutes ago, not one that has run its course.
    stale = days is not None and days >= min_days
    empty = upcoming_events == 0

    verdict = Verdict(
        source_id=source_id,
        suspected=stale and empty,
        days_since_last_event=days,
        upcoming_events=upcoming_events,
        consecutive_errors=consecutive_errors,
    )
    if not verdict.suspected:
        return verdict

    also = (
        f" It has also failed to fetch {consecutive_errors} times in a row."
        if consecutive_errors
        else " The feed still answers fine — team apps go on serving a finished"
             " season indefinitely, so that is not a sign of life."
    )
    return Verdict(
        **{
            **verdict.__dict__,
            "reason": (
                f"The most recent event in this feed was {days} days ago and "
                f"nothing is upcoming.{also} That is what a finished season "
                "looks like — but nothing has been changed, because only you "
                "know whether the team still exists."
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


def last_event_at(conn, source_id: str) -> datetime | None:
    """When the newest event calsync has ever written for this source starts.

    Read from `event_state` rather than by re-fetching: those rows are what the
    feed contained, they outlive the sync window, and answering this question
    should not cost a network round trip per source per page load.

    Cancelled rows count. A season whose last act was to cancel its final
    fixture is exactly as finished as one that played it.
    """
    row = conn.execute(
        "SELECT MAX(starts_at) AS latest FROM event_state WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return _parse(row["latest"] if row else None)


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
    return assess(
        source_id=source_id,
        last_event_at=last_event_at(conn, source_id),
        upcoming_events=upcoming_events(conn, source_id, now=now),
        consecutive_errors=consecutive_errors(conn, source_id),
        now=now,
    )


__all__ = ["Verdict", "assess", "for_source", "last_event_at",
           "MIN_DAYS_SINCE_LAST_EVENT"]
