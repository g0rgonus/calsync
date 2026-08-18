"""Telling you that something is waiting to be answered.

`/review` is only worth having if you know to look at it. A queue nobody visits
is the same as no queue — the events sit in the enrichment calendar, off the
family's phones, which is *safe* but not the point. The point is that they get
onto the right calendar quickly, and that needs a nudge.

**Once per question, not once per poll.** The poller runs every twenty minutes
and the events wait until somebody acts, so the naive version pushes the same
notification seventy times a day and is muted by lunchtime. What is recorded is
a fingerprint of *the questions themselves*: more events arriving against a
question already announced is not news, a genuinely new question is. This is the
`dormancy_notified` pattern (`seasonend.py`), for the same reason.

**Reset when the queue clears**, so the next occurrence is announced again. A
flag that latches forever would make this a once-in-a-lifetime notification.

Only questions that actually *hold* an event count. An unresolved venue is a
real gap and appears on the source page, but it does not keep anything off the
calendar, so paging somebody about it would train them to ignore the one signal
that means events are waiting.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import notify, repo
from .models import BLOCKING_DIAGNOSTICS
from .routing import slugify
from .settings import Settings
from .sync import SyncReport


@dataclass
class Outcome:
    source_id: str
    #: Events actually sitting in the enrichment collection right now.
    held: int = 0
    notified: bool = False
    errors: list[str] = field(default_factory=list)


def questions(report: SyncReport) -> tuple[str, ...]:
    """The unanswered questions that are holding events back, verbatim.

    Deliberately not every diagnostic. `unresolved_venues` is left out because
    it does not hold anything — see the module docstring.
    """
    found: list[str] = []
    for kind in BLOCKING_DIAGNOSTICS:
        found.extend(report.diagnostics.get(kind, ()))
    return tuple(sorted(set(found)))


def _fingerprint(items: tuple[str, ...]) -> str:
    """A short, stable id for a set of questions.

    Hashed rather than stored verbatim: a season's worth of unrecognised
    fixtures is a long string, and this column exists to be compared, not read.
    """
    digest = hashlib.sha256()
    for item in items:
        digest.update(item.encode())
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def _message(activity_name: str, held: int, items: tuple[str, ...]) -> tuple[str, str]:
    noun = "event" if held == 1 else "events"
    title = f"{activity_name}: {held} {noun} waiting"
    lines = [
        f"calsync could not tell which calendar {'it' if held == 1 else 'they'} "
        "belong in, so nothing has gone to the family's calendars.",
        "",
    ]
    # Bounded: a notification is a lock screen, not a report. The page has them
    # all, and that is what the link is for.
    lines += [f"· {item}" for item in items[:5]]
    if len(items) > 5:
        lines.append(f"· and {len(items) - 5} more")
    return title, "\n".join(lines)


def review(
    conn,
    source: repo.Source,
    report: SyncReport,
    *,
    secrets,
    base_url: str = "",
    sender=notify.send,
) -> Outcome:
    """Announce a source's outstanding questions, at most once each.

    Safe to call after every poll: it does nothing unless something is actually
    held *and* the questions differ from whatever was last announced.
    """
    settings = Settings.load(conn)
    collection = (
        slugify(settings.enrichment_collection) if settings.enrichment_collection else ""
    )
    held = repo.events_in_collection(conn, source.id, collection)
    outcome = Outcome(source_id=source.id, held=held)

    row = conn.execute(
        "SELECT review_notified FROM sources WHERE id = ?", (source.id,)
    ).fetchone()
    already = (row["review_notified"] if row else None) or ""

    if not held:
        # Cleared. Forget what was announced so a later occurrence is news
        # again, rather than being swallowed by a flag that never resets.
        if already:
            conn.execute(
                "UPDATE sources SET review_notified = NULL WHERE id = ?", (source.id,)
            )
            conn.commit()
        return outcome

    items = questions(report)
    if not items:
        # Held events but nothing to ask: the feed now parses cleanly and the
        # next poll will release them. Not worth a notification.
        return outcome

    signature = _fingerprint(items)
    if signature == already:
        return outcome

    config = notify.load(conn)
    if config.available(secrets):
        activity = repo.get_activity(conn, source.activity_id)
        title, message = _message(activity.name, held, items)
        try:
            sender(
                config, secrets, message, title=title,
                url=f"{base_url}/review" if base_url else None,
                url_title="Answer these",
            )
            outcome.notified = True
        except notify.NotifyError as exc:
            # Not fatal and not retried: the condition is still true at the next
            # poll, and the console shows it whether or not the push arrived.
            outcome.errors.append(str(exc))

    # Recorded even when no push went out, so a deployment with no Pushover
    # configured does not re-evaluate this on every poll forever.
    conn.execute(
        "UPDATE sources SET review_notified = ? WHERE id = ?", (signature, source.id)
    )
    conn.commit()
    return outcome
