"""Acting on a season that has finished.

`dormancy.py` decides what a source looks like; this is the only thing that does
anything about it. Two steps, deliberately far apart:

- **A month past the last event** — send one notification. The season is
  probably over, and retiring it is a judgement only the operator can make.
- **Two months past** — stop polling, and say so. By now it is over on any
  reading, and a source nobody retired in a month is a source nobody is going
  to; asking a team app for a schedule that stopped existing in May is pure
  noise from here on.

**Not every source is a replaceable season.** Most rec teams are recreated each
year under a new feed id, so a source that goes quiet is finished by definition
— which is what makes shutting it off safe. A club team configured once and kept
across seasons (docs/ONBOARDING.md names one) goes quiet every summer and comes
back, and disabling it in July would mean noticing in September. Those set
``persists_across_seasons`` in their source config: they still get the
notification, because a quiet feed is still worth knowing about, and they are
never switched off by a clock.

**Shutting off is not retiring.** It disables polling and touches nothing on the
calendar. By two months past, *every* event of a finished season is in the past,
so cancelling them would delete last spring's games from the family calendar —
the record of a season the kids actually played. That is a separate decision,
it lives behind the button on the source's page, and no timer should make it.

Notifications are sent once per stage, recorded on the source row. A push that
repeats every twenty minutes for a season that ended in May is a push nobody
reads by June.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import dormancy, notify, repo


@dataclass
class Outcome:
    source_id: str
    stage: str = dormancy.RUNNING
    notified: bool = False
    disabled: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return self.notified or self.disabled


def _message(activity_name: str, verdict: dormancy.Verdict,
             *, persists: bool = False) -> tuple[str, str]:
    if persists:
        return (
            f"{activity_name}: quiet for {verdict.days_since_last_event} days",
            "Nothing upcoming and nothing new published. This one is kept across "
            "seasons, so polling continues.",
        )
    if verdict.stage == dormancy.SHUTOFF:
        return (
            f"{activity_name}: polling stopped",
            f"No events for {verdict.days_since_last_event} days, so calsync has "
            f"stopped checking this feed. Its events are still on the calendar.",
        )
    return (
        f"{activity_name}: season looks finished",
        f"The last event was {verdict.days_since_last_event} days ago and nothing "
        f"is upcoming. Retire it when you are sure.",
    )


def review(
    conn,
    source: repo.Source,
    *,
    now: datetime,
    secrets,
    base_url: str = "",
    sender=notify.send,
) -> Outcome:
    """Check one source and escalate if it is due. Safe to call every poll.

    Does nothing at all unless a threshold has been crossed *and* that stage has
    not already been announced, so the poll loop can call it unconditionally.
    """
    verdict = dormancy.for_source(conn, source.id, now=now)
    outcome = Outcome(source_id=source.id, stage=verdict.stage)
    if verdict.stage == dormancy.RUNNING:
        return outcome

    row = conn.execute(
        "SELECT dormancy_notified FROM sources WHERE id = ?", (source.id,)
    ).fetchone()
    already = (row["dormancy_notified"] if row else None) or ""

    persists = bool((source.config or {}).get("persists_across_seasons"))

    # Shut off first, then tell them — so the notification describes something
    # that has already happened rather than something being attempted.
    if verdict.stage == dormancy.SHUTOFF and source.enabled and not persists:
        repo.set_enabled(conn, source.id, False)
        outcome.disabled = True

    if already == verdict.stage:
        return outcome

    config = notify.load(conn)
    if config.available(secrets):
        activity = repo.get_activity(conn, source.activity_id)
        title, message = _message(activity.name, verdict, persists=persists)
        try:
            sender(
                config, secrets, message, title=title,
                url=f"{base_url}/sources/{source.id}" if base_url else None,
                url_title="Open in calsync",
            )
            outcome.notified = True
        except notify.NotifyError as exc:
            # Not fatal, and not retried. The condition is still true tomorrow
            # and the console shows it whether or not the push arrived.
            outcome.errors.append(str(exc))

    # Recorded even when no push went out, so a deployment without Pushover
    # configured does not re-evaluate this every single poll forever.
    conn.execute(
        "UPDATE sources SET dormancy_notified = ? WHERE id = ?",
        (verdict.stage, source.id),
    )
    conn.commit()
    return outcome
