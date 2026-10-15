"""The adapter for a source that is pushed to rather than fetched.

The sync loop hands this the source's stored items (`repo.inbound_payload`)
where it would hand a feed adapter the bytes of a fetch, and everything after
the parse is the ordinary path. See `calsync.inbound` for what is stored and why.

Nothing is parsed out of free text, so there are no diagnostics to report: the
sender says ``"type": "game"`` rather than calsync inferring it from a title,
and a game's opponent arrives as a field. The venue is the one thing still
resolved downstream, through the alias table like any feed's.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from ..inbound import events as inbound
from ..models import Activity, Event, PollResult
from ..normalize import venue as venue_norm
from . import SourceError, register


@register("inbound")
def parse(data, activity: Activity, *, source_id: str, config: dict | None = None) -> PollResult:
    raw = data.encode() if isinstance(data, str) else data
    try:
        payload = json.loads(raw)
        items = payload["items"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"inbound store for {source_id} is unreadable: {exc}") from exc

    result = PollResult(source_id=source_id, raw_sha256=hashlib.sha256(raw).hexdigest())
    # Replaced by their parts, so emitted as cancelled: an explicit removal the
    # diff applies without the disappearance guard, never an absence. Stays in
    # the store untouched, which is what lets it come back.
    replaced = inbound.superseded(
        (i["external_id"], i["state"], bool(i.get("cancelled"))) for i in items
    )
    for item in items:
        state = item["state"]
        cancelled = bool(item.get("cancelled")) or item["external_id"] in replaced
        live = inbound.instances(source_id, item["external_id"], state,
                                 default_tz=activity.tz)
        for instance in live:
            result.events.append(_event(instance, activity, source_id, cancelled=cancelled))
        current = {i.uid for i in live}
        for gone in state.get("retired") or ():
            if gone["uid"] in current:
                continue
            result.events.append(Event(
                uid=gone["uid"],
                activity_id=activity.id,
                starts_at=datetime.fromisoformat(gone["start"]),
                ends_at=datetime.fromisoformat(gone["end"]),
                is_game=gone.get("type") == "game",
                tz=gone.get("tz") or activity.tz,
                all_day=bool(gone.get("all_day")),
                source_id=source_id,
                cancelled=True,
            ))

    if items and not result.events:
        # Every item is stored with at least one occurrence, so this cannot
        # happen honestly — and zero events reads downstream as "all cancelled".
        raise SourceError(f"inbound store for {source_id} produced no events")
    return result


def _event(instance: inbound.Instance, activity: Activity, source_id: str, *,
           cancelled: bool) -> Event:
    f = instance.fields
    is_game = f.get("type") == "game"
    return Event(
        uid=instance.uid,
        activity_id=activity.id,
        starts_at=instance.start,
        ends_at=instance.end,
        is_game=is_game,
        tz=instance.tz,
        venue=venue_norm.parse(f.get("location")),
        opponent=f.get("opponent") if is_game else None,
        home=f.get("home") if is_game else None,
        detail=f.get("label"),
        body=f.get("notes"),
        url=f.get("url"),
        source_id=source_id,
        source_category=f.get("type"),
        content_hash=inbound.content_hash(instance),
        all_day=instance.all_day,
        cancelled=cancelled,
    )
