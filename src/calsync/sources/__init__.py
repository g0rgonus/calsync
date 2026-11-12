"""Source adapters — where events come from.

Registered by the ``kind`` stored in ``sources.kind``, so adding a source is a
row in the database rather than a branch in the sync loop. Mirrors the target
registry deliberately: both ends of the pipeline are pluggable.
"""

from __future__ import annotations

from typing import Callable

from ..models import Activity, PollResult

#: kind -> parser. A parser takes the raw payload and returns a PollResult, and
#: must raise rather than return zero events for an empty or unreadable feed.
_PARSERS: dict[str, Callable[..., PollResult]] = {}

#: Kinds whose parser takes the sync window, because it expands repeating
#: series and needs an edge for a rule with no end.
_WINDOWED: set[str] = set()


class SourceError(RuntimeError):
    """The source could not be handled."""


def register(kind: str, *, windowed: bool = False):
    def wrap(fn):
        _PARSERS[kind] = fn
        if windowed:
            _WINDOWED.add(kind)
        return fn

    return wrap


def available() -> list[str]:
    return sorted(_PARSERS)


#: Kinds that are pushed to rather than fetched. They have no URL, so the sync
#: loop reads their store instead (`repo.inbound_payload`), and onboarding
#: offers them as a choice of their own rather than in the feed-kind picker.
PUSHED = frozenset({"inbound"})

#: Fetched kinds with no fixtures: a club or school calendar has no games, so
#: promotion waits for events rather than a game (`SyncReport.promotable`).
EVENTS_ONLY = frozenset({"calendar-feed"})


def fetched() -> list[str]:
    """The kinds a pasted feed URL can be read as."""
    return [kind for kind in available() if kind not in PUSHED]


def parse(
    kind: str,
    data: str | bytes,
    activity: Activity,
    *,
    source_id: str,
    config: dict | None = None,
    window: tuple | None = None,
) -> PollResult:
    """Parse a payload with this source's adapter.

    ``config`` is the source's own ``config`` column. Until now it was stored
    and never delivered, which made every key in it exactly the kind of
    configuration `config.py` refuses to drop silently — preserved, documented,
    and quietly doing nothing.
    """
    try:
        parser = _PARSERS[kind]
    except KeyError:
        raise SourceError(
            f"unknown source kind {kind!r}; available: {', '.join(available()) or 'none'}"
        ) from None
    extra = {"window": window} if kind in _WINDOWED else {}
    return parser(data, activity, source_id=source_id, config=config, **extra)


from . import calendar_feed, inbound, player360, teamreach  # noqa: E402,F401  (populate the registry)
