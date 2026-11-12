"""Telling someone a calendar feed published a category nobody has placed.

The event is already on a calendar (the source's default), so this is news,
not a question holding anything back. One push per category, never one per
poll: the categories already announced are kept in `sources.category_notified`,
apart from `review_notified`, so neither announcement silences the other.

While a source is staged nothing is pushed and its categories are recorded as
announced: the person onboarding it is looking at the category table already,
and the first poll would otherwise send one push for every category.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import notify, repo, routing
from .settings import Settings


@dataclass
class Outcome:
    source_id: str
    new: tuple[str, ...] = ()
    notified: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)


def new(report, config: dict | None) -> tuple[str, ...]:
    """Categories this poll saw that have no destination set yet."""
    return tuple(sorted(label for label in report.categories
                        if routing.is_new(label, config)))


def calendars(conn, config: dict | None) -> list[str]:
    """The calendars a category or rule may name: those already written to,
    outside any holding calendar, and those the source already names."""
    config = config or {}
    settings = Settings.load(conn)
    holding = {s.staging_collection for s in repo.list_sources(conn) if s.staging_collection}
    if settings.enrichment_collection:
        holding.add(routing.slugify(settings.enrichment_collection))
    found = set(repo.written_collections(conn, exclude=holding))
    found |= {config["default_calendar"]} if config.get("default_calendar") else set()
    found |= {v for v in (config.get("categories") or {}).values() if v and v != routing.SKIP}
    found |= {r["destination"] for r in config.get("rules") or []
              if r["destination"] != routing.SKIP}
    return sorted(found)


def _key(label: str) -> str:
    return " ".join(label.split()).casefold()


def message(activity_name: str, label: str, count: int, calendar: str) -> tuple[str, str]:
    """What already happened, because the event is already on a calendar."""
    noun = "event" if count == 1 else "events"
    return (
        f'{activity_name}: new category "{label}"',
        f"{count} {noun} added to the {calendar} calendar. "
        "Keep it there, move it, or skip it.",
    )


def review(conn, source: repo.Source, report, *, secrets, base_url: str = "",
           sender=notify.send) -> Outcome:
    """Announce each new category once. Safe to call after every poll."""
    config = source.config or {}
    outcome = Outcome(source_id=source.id)
    if not config.get("default_calendar") or report.status == "error":
        # Not routed by category, or a failed fetch that saw nothing: forgetting
        # what was announced on an empty poll would announce it all again.
        return outcome

    outcome.new = new(report, config)
    row = conn.execute("SELECT category_notified FROM sources WHERE id = ?",
                       (source.id,)).fetchone()
    announced = set(json.loads((row["category_notified"] if row else None) or "[]"))
    fresh = [label for label in outcome.new if _key(label) not in announced]

    if fresh and not source.staging_collection:
        settings = notify.load(conn)
        if settings.available(secrets):
            activity = repo.get_activity(conn, source.activity_id)
            for label in fresh:
                title, body = message(activity.name, label, report.categories[label],
                                      config["default_calendar"])
                try:
                    sender(settings, secrets, body, title=title,
                           url=f"{base_url}/sources/{source.id}#categories"
                           if base_url else None,
                           url_title="Choose where it goes")
                    outcome.notified += (label,)
                except notify.NotifyError as exc:
                    # Not retried: the category stays marked new on the source
                    # page and on /review whether or not the push arrived.
                    outcome.errors.append(str(exc))

    # Only what is still new, so a category a person placed and later removed
    # from the config is news again.
    keep = sorted(_key(label) for label in outcome.new)
    if keep != sorted(announced):
        conn.execute("UPDATE sources SET category_notified = ? WHERE id = ?",
                     (json.dumps(keep) if keep else None, source.id))
        conn.commit()
    return outcome
