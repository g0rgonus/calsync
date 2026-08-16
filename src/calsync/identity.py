"""Derive a *stable* UID for an event.

The UID is the primary key of ``event_state`` and the resource name at every
target, so it has to mean "the same real-world event" across polls. Feeds do not
agree on this:

- **Player360** emits ``360Player-event-4716716`` — the id is the whole UID and
  it is stable. ``passthrough``.
- **One observed source** emits ``<event_id><ISO timestamp with microseconds>``,
  where the timestamp is *generation* time. The same event gets a fresh UID on
  every poll, and the stable identity is the leading id only. ``extract``.
- **Some feeds have no usable id at all**, and identity has to be reconstructed
  from content. ``synthesize``.

Getting this wrong does not look like an error. It looks like the whole season
being created again on every poll, with the previous copy orphaned in the
calendar — which is why :func:`calsync.diff.diff_poll` also guards against total
identity turnover rather than trusting adapters to be right.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

PASSTHROUGH = "passthrough"
EXTRACT = "extract"
SYNTHESIZE = "synthesize"


class IdentityError(RuntimeError):
    """A UID could not be derived. Never fall back to the raw value silently —
    an unstable UID duplicates a calendar, so failing loudly is the safe end."""


def passthrough(raw_uid: str) -> str:
    if not raw_uid or not raw_uid.strip():
        raise IdentityError("feed supplied an empty UID")
    return raw_uid.strip()


def extract(raw_uid: str, pattern: str) -> str:
    """Pull the stable part out of a composite UID.

    ``pattern`` must capture it, either as a group named ``id`` or as group 1 —
    e.g. ``^(?P<id>\\d+)`` against ``127823172026-04-24T17:47:04.859629``.
    """
    match = re.search(pattern, raw_uid or "")
    if match is None:
        raise IdentityError(f"uid pattern {pattern!r} did not match {raw_uid!r}")
    try:
        value = match.group("id")
    except IndexError:
        value = match.group(1) if match.groups() else match.group(0)
    if not value:
        raise IdentityError(f"uid pattern {pattern!r} captured nothing from {raw_uid!r}")
    return value


def synthesize(*, activity_id: str, starts_at: datetime, summary: str) -> str:
    """Content-derived identity, for feeds with no usable id.

    Start time plus normalized summary within one activity. Deliberately
    excludes venue and description: a venue correction is an *update* to an
    event, and including it would make the event look like a different one.

    The tradeoff is real and worth stating — a rescheduled event synthesizes a
    new UID, so it reads as a cancellation plus a creation rather than a move.
    Prefer ``extract`` whenever the feed offers any stable id at all.
    """
    digest = hashlib.sha256()
    for part in (activity_id, starts_at.astimezone().isoformat(), " ".join(summary.split()).casefold()):
        digest.update(part.encode())
        digest.update(b"\x1e")
    return f"calsync-{digest.hexdigest()[:32]}"


def resolve(
    policy: dict | None,
    *,
    raw_uid: str | None,
    activity_id: str,
    starts_at: datetime,
    summary: str,
) -> str:
    """Apply a source's configured ``uid`` policy.

    Policy is a dict from ``sources.config``: ``{"mode": "extract",
    "pattern": "^(?P<id>\\\\d+)"}``. Absent policy means passthrough, which is
    correct only for feeds verified to have stable UIDs.
    """
    policy = policy or {}
    mode = policy.get("mode", PASSTHROUGH)

    if mode == PASSTHROUGH:
        return passthrough(raw_uid or "")
    if mode == EXTRACT:
        pattern = policy.get("pattern")
        if not pattern:
            raise IdentityError("uid policy 'extract' requires a 'pattern'")
        return extract(raw_uid or "", pattern)
    if mode == SYNTHESIZE:
        return synthesize(activity_id=activity_id, starts_at=starts_at, summary=summary)
    raise IdentityError(
        f"unknown uid policy {mode!r}; expected one of "
        f"{PASSTHROUGH}, {EXTRACT}, {SYNTHESIZE}"
    )
