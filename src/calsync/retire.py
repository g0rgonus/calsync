"""Take a finished season off the family's calendars.

Rec teams are recreated every season under a new name with a new feed id, so a
source does not gradually go quiet — it dies, permanently, usually while its
last few events are still sitting in the calendar. Somebody has to remove them,
and the tempting way to do that is to delete the source row, which is the one
thing that must never happen: `event_state` cascades with it, calsync forgets it
ever wrote those events, and they stay in the shared calendar forever with
nothing tracking them. Deleting is not removing.

So retiring is the reverse of a sync, and it borrows the sync loop's ordering
rule exactly (docs/ONBOARDING.md, `sync.py`): **cancel at the target first, then
record it**. The other order would mark an event gone while it is still on
somebody's phone, and nothing would ever retry.

The step that is easy to leave out is disabling the source. `known_hashes`
excludes cancelled rows, so a source that is cancelled but still enabled sees
every event in its feed as new on the very next poll and puts the whole season
straight back. Cancelling without disabling is not a partial retirement, it is a
no-op with extra steps — so this does both, or neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import repo
from .targets import TargetError, TargetRef


@dataclass
class RetireReport:
    source_id: str
    cancelled: int = 0
    already_gone: int = 0
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
        if self.already_gone:
            parts.append(f"{self.already_gone} already gone")
        if self.disabled:
            parts.append("polling stopped")
        parts.extend(f"ERROR: {e}" for e in self.errors)
        return ", ".join(parts)


def retire_source(conn, source: repo.Source, target) -> RetireReport:
    """Cancel everything this source put on the calendar, and stop polling it.

    Leaves the source row and its `event_state` tombstones in place. They cost
    nothing and they are the record that these events were ours — which is what
    stops a resurrected UID from being adopted a second time.
    """
    report = RetireReport(source_id=source.id)
    states = repo.event_states(conn, source.id)

    for uid, state in states.items():
        if state.cancelled:
            report.already_gone += 1
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


def forget_source(conn, source_id: str) -> None:
    """Drop the row for good. Only safe once nothing of ours is live.

    The caller must have retired it first; :func:`live_events` is the check.
    This exists for the end of a season two seasons ago, when the tombstones
    have outlived their usefulness and the dashboard is the thing being tidied.
    """
    live = live_events(conn, source_id)
    if live:
        raise ValueError(
            f"{source_id} still has {live} event(s) on the calendar. Retire it "
            "first — deleting the row now would leave them there with nothing "
            "tracking them."
        )
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()


def live_events(conn, source_id: str) -> int:
    return repo.tracked_events(conn, source_id)
