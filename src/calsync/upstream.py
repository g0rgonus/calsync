"""Telling somebody the feed changed an event without saying what.

Separate from `enrichment.py`, which is about questions that *hold events off
the calendar*. Nothing is held here. The event is on the family's calendar,
exactly as it was, and the only thing calsync knows is that the publisher
rewrote it — because `LAST-MODIFIED` moved to before the event while every field
calsync reads stayed identical (`sync._note_upstream_edit`).

Observed once, on 2026-08-20, and it was a cancelled practice: the app said
"This event has been canceled" while the `.ics` went on exporting an ordinary
practice (docs/sources/player360.md, Trap 2). So this is the only notice a
family gets that a cancellation happened, and it still cannot say so — one of
the two pre-`DTEND` edits in that sample was not a cancellation.

It therefore **notifies and files, and changes no calendar**. Guessing
"cancelled" from a timestamp would be a delete on a shared calendar, decided by
inference, which is the operation this project is most careful about.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import notify, repo


@dataclass
class Outcome:
    source_id: str
    edited: int = 0
    notified: bool = False
    errors: list[str] = field(default_factory=list)


def _fingerprint(uids: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for uid in sorted(uids):
        digest.update(uid.encode())
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def _message(activity_name: str, count: int) -> tuple[str, str]:
    subject = "event" if count == 1 else "events"
    return (
        f"{activity_name}: {count} {subject} changed at the source",
        f"The publisher rewrote {count} {subject} without changing anything "
        "calsync can read, so what changed is only visible in the team's own "
        "app. A cancellation looks exactly like this — the calendar still shows "
        "the event, because the feed still publishes it.",
    )


def review(
    conn,
    source: repo.Source,
    *,
    secrets,
    base_url: str = "",
    sender=notify.send,
) -> Outcome:
    """Announce this source's unexplained edits, at most once per set.

    Same shape as `enrichment.review`, and for the same reason: the poller runs
    every twenty minutes, and a per-poll push is muted by lunchtime. Its own
    column rather than a shared one, so a queue that opens while another kind of
    notice is outstanding is still announced.
    """
    pending = tuple(
        row["uid"] for row in repo.pending_upstream_edits(conn)
        if row["source_id"] == source.id
    )
    outcome = Outcome(source_id=source.id, edited=len(pending))

    row = conn.execute(
        "SELECT edits_notified FROM sources WHERE id = ?", (source.id,)
    ).fetchone()
    already = (row["edits_notified"] if row else None) or ""

    if not pending:
        # Cleared, so a later one is news again rather than being swallowed by
        # a flag that never resets.
        if already:
            conn.execute(
                "UPDATE sources SET edits_notified = NULL WHERE id = ?", (source.id,)
            )
            conn.commit()
        return outcome

    signature = _fingerprint(pending)
    if signature == already:
        return outcome

    config = notify.load(conn)
    if config.available(secrets):
        activity = repo.get_activity(conn, source.activity_id)
        title, message = _message(activity.name, len(pending))
        try:
            sender(
                config, secrets, message, title=title,
                url=f"{base_url}/review" if base_url else None,
                url_title="See which",
            )
            outcome.notified = True
        except notify.NotifyError as exc:
            # Not fatal and not retried: the row is still flagged at the next
            # poll, and the console shows it whether or not the push arrived.
            outcome.errors.append(str(exc))

    conn.execute(
        "UPDATE sources SET edits_notified = ? WHERE id = ?", (signature, source.id)
    )
    conn.commit()
    return outcome
