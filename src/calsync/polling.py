"""When to poll each source next.

Pulled out of the poll loop because the loop itself is an infinite `while` with
a sleep in it — untestable by construction — while the decision it makes every
tick is a pure function of the last outcome. The bug this fixes lived in the one
line that was never exercised.

Until now every source was rescheduled at its full rate whatever happened. That
is fine for a working feed and wrong for the common case: a rec season ends, the
team is replaced, and its feed 404s *forever*. calsync would ask again every
twenty minutes for the rest of time, writing a `poll_runs` row and an error each
go, hammering somebody else's host for a team that no longer exists, and burying
every real error in the status output under thousands of identical ones.

Backing off does not decide anything about dormancy — a source that stops
answering might be a dead season or might be a host having a bad afternoon, and
telling those apart is a separate question this deliberately does not answer. It
just stops asking quite so often, and keeps asking forever, so recovery is
automatic whichever it turns out to be.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Never wait longer than this between attempts, however long a feed has been
#: failing. A source that comes back should be picked up the same day without
#: anyone restarting the poller.
MAX_INTERVAL_S = 6 * 3600

#: Never poll faster than this, whatever a source asks for. Feeds advertise
#: refresh intervals of five minutes; there is no reason to be the reason a
#: small league's host falls over.
MIN_INTERVAL_S = 60


def next_delay(interval_s: int, failures: int, *, cap: int = MAX_INTERVAL_S) -> int:
    """Seconds to wait before trying this source again.

    The first failure costs nothing — a single blip should not delay the next
    attempt, because most of them are transient and the feed is usually back
    before the normal interval has even elapsed. It doubles from there, so a
    genuinely dead feed decays to a few attempts a day rather than seventy.
    """
    interval = max(int(interval_s), MIN_INTERVAL_S)
    if failures <= 1:
        return interval
    # Cheap and bounded; 2**failures on a source failing for a month would
    # otherwise be an integer with a lot of digits in it.
    doublings = min(failures - 1, 20)
    return min(interval * (2 ** doublings), cap)


@dataclass
class Schedule:
    """Which sources are due, and how badly each one is doing.

    Held in memory on purpose. Due-times do not survive a restart, so a restart
    polls everything once — harmless, and cheaper than persisting a schedule.
    Failure counts are rebuilt the same way: a restart forgives, which is the
    right bias for something whose whole job is to keep trying.
    """

    due: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)

    def is_due(self, source_id: str, now: float) -> bool:
        return now >= self.due.get(source_id, 0.0)

    def record(self, source_id: str, *, status: str, interval_s: int, now: float) -> int:
        """Note an outcome and schedule the next attempt. Returns the delay.

        ``held`` counts as success. The guard tripping means the fetch and the
        parse both worked and the feed is alive — it is the *content* that
        looked wrong. Treating it as a failure would slow down exactly the
        source somebody needs to look at soonest.
        """
        if status == "error":
            self.failures[source_id] = self.failures.get(source_id, 0) + 1
        else:
            self.failures.pop(source_id, None)

        delay = next_delay(interval_s, self.failures.get(source_id, 0))
        self.due[source_id] = now + delay
        return delay

    def forget(self, source_id: str) -> None:
        """Drop a source that is no longer enabled, so its state cannot leak
        into a later source that reuses the id."""
        self.due.pop(source_id, None)
        self.failures.pop(source_id, None)

    def struggling(self) -> dict[str, int]:
        return dict(self.failures)
