"""How often a failing source is retried.

Every source used to be rescheduled at its full rate whatever happened, which is
right for a working feed and wrong for the common case: a rec season ends, the
team is replaced, and its feed 404s forever. calsync would ask again every
twenty minutes for the rest of time.
"""

from __future__ import annotations

from calsync.polling import MAX_INTERVAL_S, Schedule, next_delay

TWENTY_MINUTES = 1200


def test_a_healthy_source_keeps_its_own_interval():
    assert next_delay(TWENTY_MINUTES, failures=0) == TWENTY_MINUTES


def test_the_first_failure_costs_nothing():
    """Most failures are a blip and the feed is back before the next attempt.

    Penalising the first one delays recovery for the overwhelmingly common case
    to punish the rare one.
    """
    assert next_delay(TWENTY_MINUTES, failures=1) == TWENTY_MINUTES


def test_it_doubles_from_the_second_failure():
    assert next_delay(TWENTY_MINUTES, failures=2) == 2 * TWENTY_MINUTES
    assert next_delay(TWENTY_MINUTES, failures=3) == 4 * TWENTY_MINUTES


def test_a_dead_season_decays_to_a_few_attempts_a_day():
    """The whole point: 72 requests a day for a team that no longer exists."""
    assert next_delay(TWENTY_MINUTES, failures=20) == MAX_INTERVAL_S


def test_it_never_waits_longer_than_the_cap():
    """A feed that comes back is picked up the same day without a restart."""
    assert next_delay(TWENTY_MINUTES, failures=10_000) == MAX_INTERVAL_S


def test_a_source_asking_to_be_polled_constantly_is_slowed_down():
    assert next_delay(5, failures=0) == 60


# --- the schedule -----------------------------------------------------------


def test_success_clears_the_backoff():
    schedule = Schedule()
    for _ in range(4):
        schedule.record("s", status="error", interval_s=TWENTY_MINUTES, now=0)
    assert schedule.struggling()["s"] == 4

    delay = schedule.record("s", status="ok", interval_s=TWENTY_MINUTES, now=0)
    assert schedule.struggling() == {}
    assert delay == TWENTY_MINUTES


def test_a_held_poll_is_not_a_failure():
    """The guard tripping means the fetch and parse both worked.

    It is the content that looked wrong — and that is the source somebody needs
    to look at soonest, so it is the last one to slow down.
    """
    schedule = Schedule()
    schedule.record("s", status="error", interval_s=TWENTY_MINUTES, now=0)
    schedule.record("s", status="error", interval_s=TWENTY_MINUTES, now=0)

    delay = schedule.record("s", status="held", interval_s=TWENTY_MINUTES, now=0)
    assert schedule.struggling() == {}
    assert delay == TWENTY_MINUTES


def test_a_source_is_not_due_until_its_delay_has_passed():
    schedule = Schedule()
    schedule.record("s", status="ok", interval_s=TWENTY_MINUTES, now=1000)

    assert not schedule.is_due("s", 1000 + TWENTY_MINUTES - 1)
    assert schedule.is_due("s", 1000 + TWENTY_MINUTES)


def test_an_unknown_source_is_due_immediately():
    """A restart polls everything once, which is cheaper than persisting state."""
    assert Schedule().is_due("never-seen", 0)


def test_forgetting_a_source_clears_its_backoff():
    """A retired id that is later reused must not inherit its penalty."""
    schedule = Schedule()
    for _ in range(5):
        schedule.record("s", status="error", interval_s=TWENTY_MINUTES, now=0)

    schedule.forget("s")
    assert schedule.struggling() == {}
    assert schedule.is_due("s", 0)
