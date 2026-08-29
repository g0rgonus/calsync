"""Put a warm-up on the calendar before every game.

A team that wants you there 45 minutes early has a real obligation the feed
never publishes: the coach says it once at the start of the season and no
export has ever carried it. So calsync synthesizes it — one event per game,
ending at kick-off, at the same place — from a single number per team.

Three decisions here are load-bearing somewhere else in the pipeline:

- **A warm-up is not a game.** ``is_game`` is False, which is what makes
  `routing.collection_for` file it with the practices under the default
  ``{type}`` template. That is the honest answer as well as the useful one:
  "who are we playing" and "when do I have to be somewhere" are different
  questions, and a warm-up only ever answers the second.
- **It is a shadow of its game, not evidence from the feed.** Its UID is
  derived from the game's, so it is created, updated, moved and cancelled
  exactly when the game is — and `sync.sync_source` tells `diff.diff_poll` not
  to count it toward the disappearance guard. Counting it would double every
  disappearance in a games-only feed, tripping at two real cancellations a
  threshold that was measured against four.
- **The game learns its own arrival time too** (`Event.arrive_at`, which
  `render.build_body` prints). The same fact stated in both places somebody
  might look, because somebody reading the game event should not have to know a
  second event exists.

Switching it off is an ordinary cancellation: the warm-ups stop being
generated, vanish from the poll and are deleted on the next run. The guard
exemption above is also what lets that happen without holding the sync — a
whole season's worth of synthetic events disappearing at once is exactly the
shape the guard exists to refuse, and exactly the one case where it is meant.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from .models import Event

#: Namespace for a UID calsync minted rather than read. It deliberately shares
#: the ``calsync-`` prefix `identity.synthesize` uses: a UID that did not come
#: from a feed says so in its own text, which is what lets :func:`is_synthetic`
#: answer for a uid read back out of ``event_state``, where there is a string
#: and no event to ask.
PREFIX = "calsync-warmup-"


def uid_for(parent_uid: str) -> str:
    return f"{PREFIX}{parent_uid}"


def is_synthetic(uid: str) -> bool:
    """Did calsync mint this UID, rather than read it from a feed?"""
    return uid.startswith(PREFIX)


def _content_hash(game: Event, minutes: int) -> str:
    """A hash that moves when the game moves *or* when the offset changes.

    Derived from the parent's hash rather than computed over the warm-up's own
    fields, so whatever the adapter counts as a change to a game counts as a
    change to its warm-up, with no second definition to keep in step.
    """
    seed = game.content_hash or f"{game.uid}@{game.starts_at.isoformat()}"
    return hashlib.sha256(f"{seed}\x1e{minutes}".encode()).hexdigest()[:32]


def _warmup(game: Event, minutes: int) -> Event:
    return Event(
        uid=uid_for(game.uid),
        activity_id=game.activity_id,
        starts_at=game.starts_at - timedelta(minutes=minutes),
        ends_at=game.starts_at,
        # Not a game. This one field is what sends it to the practice calendar.
        is_game=False,
        tz=game.tz,
        venue=game.venue,
        # Kept so the title can name the fixture. Two games on one Saturday
        # otherwise give two warm-ups that read as the same row twice.
        opponent=game.opponent,
        home=game.home,
        detail=game.detail,
        # The coach's note belongs to the fixture. Repeating it here would put
        # a description of the game on an event that is not the game;
        # `render.build_body` states what this is instead.
        body=None,
        url=game.url,
        source_id=game.source_id,
        # The feed published no category for an event it does not know exists.
        source_category=None,
        content_hash=_content_hash(game, minutes),
        kit=game.kit,
        # Inherited, so a held game and its warm-up are never split across the
        # enrichment collection and a real one. A warm-up for a game nobody can
        # see does not belong in front of the family on its own.
        unresolved=game.unresolved,
        warmup_for=game.uid,
    )


def expand(events: list[Event], *, minutes: int) -> list[Event]:
    """``events``, plus a warm-up before each timed game.

    Also sets ``arrive_at`` on the games themselves, in place — the same
    mutation `sync._enrich_venue` performs and for the same reason: this runs
    inside the sync layer on events an adapter has just produced, which nothing
    else holds a reference to.

    An all-day game gets nothing. "45 minutes before" an event whose start is
    local midnight is 23:15 the previous evening, for a kick-off nobody has
    published yet — the same reason `render.render` withholds an alarm from one.
    """
    if minutes <= 0:
        return events

    expanded: list[Event] = []
    for event in events:
        expanded.append(event)
        if not event.is_game or event.all_day:
            continue
        event.arrive_at = event.starts_at - timedelta(minutes=minutes)
        expanded.append(_warmup(event, minutes))
    return expanded
