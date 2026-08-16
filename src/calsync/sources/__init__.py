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


class SourceError(RuntimeError):
    """The source could not be handled."""


def register(kind: str):
    def wrap(fn):
        _PARSERS[kind] = fn
        return fn

    return wrap


def available() -> list[str]:
    return sorted(_PARSERS)


def parse(
    kind: str, data: str | bytes, activity: Activity, *, source_id: str
) -> PollResult:
    try:
        parser = _PARSERS[kind]
    except KeyError:
        raise SourceError(
            f"unknown source kind {kind!r}; available: {', '.join(available()) or 'none'}"
        ) from None
    return parser(data, activity, source_id=source_id)


from . import player360, teamreach  # noqa: E402,F401  (populate the registry)
