"""Stop polling a source, and clear what it still has coming.

Rec teams are recreated every season under a new name with a new feed id, so a
source does not gradually go quiet — it dies, permanently, usually while its
last few events are still sitting in the calendar. Somebody has to deal with it,
and the tempting way is to delete the source row, which is the one thing that
must never happen: `event_state` cascades with it, calsync forgets it ever wrote
those events, and they stay in the shared calendar forever with nothing tracking
them. Deleting is not removing.

**Only what has not happened yet comes off.** This used to cancel every event a
source had ever written, which meant retiring a finished season deleted the
record of a season the kids actually played — last spring's games, gone from the
family calendar, because the feed serving them stopped being interesting. A
schedule and a history are not the same thing. `seasonend.py` already refused to
let a *timer* do that; it made no sense for the button it defers to to do it
unconditionally instead, especially since the nudge fires a month past the last
event, by which point there is nothing upcoming left to clear at all.

So the rule is the one that is right in both cases somebody retires:

- **The season ended.** Everything is in the past. Nothing is cancelled and
  polling stops — which is all that was ever wanted.
- **The team was abandoned mid-season**, or the wrong feed was onboarded. The
  events still to come are phantoms; those are exactly what comes off.

Retiring is otherwise the reverse of a sync, and it borrows the sync loop's
ordering rule exactly (docs/ONBOARDING.md, `sync.py`): **cancel at the target
first, then record it**. The other order would mark an event gone while it is
still on somebody's phone, and nothing would ever retry.

The step that is easy to leave out is disabling the source. `known_hashes`
excludes cancelled rows, so a source that is cancelled but still enabled sees
its upcoming events as new on the very next poll and puts them straight back.
Cancelling without disabling is not a partial retirement, it is a no-op with
extra steps — so this does both, or neither. Past events need no such protection:
both sides of the diff are filtered to the sync window, so an event old enough to
be kept is invisible to it either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import repo
from .targets import TargetError, TargetRef


@dataclass
class RetireReport:
    source_id: str
    cancelled: int = 0
    already_gone: int = 0
    #: Events left on the calendar because they have already happened. Counted
    #: and reported rather than passed over silently: "retired, 0 events
    #: removed" reads like nothing happened when what it means is that the whole
    #: season is safely in the past.
    kept: int = 0
    errors: list[str] = field(default_factory=list)
    disabled: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def removable(self) -> bool:
        """Safe to drop the source row entirely?

        Only once nothing of ours is left on the calendar. While a single event
        is still live, the `event_state` row is the only record that calsync put
        it there, and dropping it strands the event permanently.
        """
        return self.ok and self.disabled

    def line(self) -> str:
        parts = [f"{self.source_id}: {self.cancelled} cancelled"]
        if self.kept:
            parts.append(f"{self.kept} past events kept")
        if self.already_gone:
            parts.append(f"{self.already_gone} already gone")
        if self.disabled:
            parts.append("polling stopped")
        parts.extend(f"ERROR: {e}" for e in self.errors)
        return ", ".join(parts)


def retire_source(conn, source: repo.Source, target, *, now: datetime) -> RetireReport:
    """Clear what this source still has coming, and stop polling it.

    Events that have already started are left exactly where they are — see the
    module docstring. ``now`` is required rather than defaulted so that a caller
    pinning the clock (``--now``, the console's ``clock``) cannot accidentally
    retire against the wall clock and take a different set of events off.

    Leaves the source row and its `event_state` rows in place. They cost nothing
    and they are the record that these events were ours — which is what stops a
    resurrected UID from being adopted a second time.
    """
    report = RetireReport(source_id=source.id)
    states = repo.event_states(conn, source.id)

    for uid, state in states.items():
        if state.cancelled:
            report.already_gone += 1
            continue
        if not _upcoming(state.starts_at, now):
            # It happened. Removing it now would delete the record of a game
            # that was played, which is not what anybody means by retiring.
            report.kept += 1
            continue
        try:
            target.cancel(
                TargetRef(
                    collection=state.collection,
                    remote_id=state.remote_id or uid,
                    etag=state.remote_etag,
                )
            )
        except TargetError as exc:
            # Keep going: one unreachable event should not strand the other
            # forty. The source stays enabled so a later run picks these up.
            report.errors.append(f"{uid}: {exc}")
            continue
        repo.mark_event_cancelled(conn, uid)
        report.cancelled += 1

    # Only when the calendar is genuinely clear. Disabling early would leave
    # events behind with no poller left to remove them.
    if report.ok:
        repo.set_enabled(conn, source.id, False)
        report.disabled = True
        repo.record_poll_run(
            conn, source_id=source.id, status="ok",
            detail=f"retired: {report.cancelled} events cancelled, polling stopped",
        )

    conn.commit()
    return report


def forget_source(conn, source_id: str, *, now: datetime) -> None:
    """Drop the row for good. Only safe once nothing of ours is still to come.

    The caller must have retired it first; :func:`live_events` is the check.
    This exists for the end of a season two seasons ago, when the row has
    outlived its usefulness and the dashboard is the thing being tidied.

    Past events left on the calendar do not block this, which is the whole point
    of keeping them: nothing will ever poll this feed again, so there is nothing
    left for calsync to do to a game that was played in April. An *upcoming*
    event is different — dropping the row while one is still to come strands it
    on the family's calendar with nothing able to move or remove it.
    """
    live = live_events(conn, source_id, now=now)
    if live:
        raise ValueError(
            f"{source_id} still has {live} event(s) still to come. Retire it "
            "first — deleting the row now would leave them there with nothing "
            "tracking them."
        )
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()


def live_events(conn, source_id: str, *, now: datetime) -> int:
    """Events this source has on the calendar that have not happened yet.

    Deliberately not `repo.tracked_events`, which counts everything uncancelled
    and is the right number for "what is on the calendar from this source". This
    one answers a narrower question — is there anything left that calsync might
    still need to act on — and a game last April is not that.
    """
    return sum(
        1
        for state in repo.event_states(conn, source_id).values()
        if not state.cancelled and _upcoming(state.starts_at, now)
    )


def _upcoming(starts_at: str, now: datetime) -> bool:
    """Has this event not started yet?

    Parsed rather than compared as text: `starts_at` is stored as an ISO string
    whose offset is whatever the feed carried, so "2026-04-01T09:00:00-04:00"
    and "2026-04-01T12:00:00+00:00" are the same instant and sort differently.
    An unparseable value is treated as upcoming, because the safe failure here is
    to offer to remove an event rather than to silently keep one forever.
    """
    try:
        return datetime.fromisoformat(starts_at) >= now
    except ValueError:
        return True
