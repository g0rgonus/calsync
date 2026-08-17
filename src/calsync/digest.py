"""What's on tomorrow, as a message.

The obvious implementations are both wrong, and for reasons the project has
already written down:

- **Read it back from the calendar.** `docs/API.md` refuses this for Hermes and
  the refusal applies here too — the calendar holds *renders*, not data. Pulling
  "Patrick ⚽️ vs Strikers" back apart into a child and an opponent is
  reverse-engineering a string we generated ourselves, and it re-breaks every
  time the naming convention changes.
- **Read it out of `event_state`.** There is nothing to read. That table holds
  hashes and placement, because the title is a render and is deliberately never
  stored.

So a digest re-derives from the feeds, exactly as a sync does, and renders the
same titles through the same code — which is what makes it agree with the
calendar rather than approximate it. It costs one fetch per source, once a day.

**It writes nothing.** Not to the database, not to the calendar, not a poll_runs
row. A digest is a read, and a read that quietly advanced sync state would make
"tell me what's on" a thing you had to think twice about running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import repo, sources
from .fetch import http_fetch, render_url
from .render import render
from .settings import Settings


@dataclass(frozen=True)
class Entry:
    starts_at: datetime
    tz: str
    title: str
    venue: str | None
    is_game: bool

    @property
    def local(self) -> datetime:
        """In the *venue's* timezone, which is what people plan around.

        Same rule the event bodies follow: a travelling parent reading a time
        rendered in their own timezone reads a time that is not the start time.
        """
        return self.starts_at.astimezone(ZoneInfo(self.tz))

    def line(self) -> str:
        when = self.local.strftime("%H:%M")
        where = f" · {self.venue}" if self.venue else ""
        return f"{when}  {self.title}{where}"


@dataclass
class Digest:
    entries: list[Entry]
    #: Sources that could not be read. Named rather than dropped: a digest that
    #: silently omits a team reads as "nothing on today", which is the one
    #: wrong answer a schedule message can give.
    unavailable: list[str]
    starts: datetime
    ends: datetime

    @property
    def empty(self) -> bool:
        return not self.entries

    def text(self) -> str:
        day = self.starts.strftime("%A %-d %B")
        if self.empty and not self.unavailable:
            return f"**{day}** — nothing on."

        lines = [f"**{day}**"]
        lines += [f"- {e.line()}" for e in sorted(self.entries, key=lambda e: e.starts_at)]
        if self.unavailable:
            lines.append("")
            lines.append(
                "Could not read: " + ", ".join(sorted(self.unavailable))
                + " — so this may be incomplete."
            )
        return "\n".join(lines)


def collect(
    conn,
    *,
    now: datetime,
    hours: int = 24,
    secrets=None,
    fetcher=http_fetch,
) -> Digest:
    """Everything starting in the next ``hours``, rendered.

    Disabled sources are skipped: paused or retired means the events are not on
    the calendar, and a digest that disagrees with the calendar is worse than no
    digest.
    """
    settings = Settings.load(conn)
    starts, ends = now, now + timedelta(hours=hours)

    entries: list[Entry] = []
    unavailable: list[str] = []

    for source in repo.list_sources(conn, enabled_only=True):
        activity = repo.get_activity(conn, source.activity_id)
        children = [repo.get_child(conn, activity.child_id)]
        try:
            if not source.url_template:
                raise ValueError("no url_template")
            raw = fetcher(render_url(source.url_template, secrets=secrets, now=now))
            result = sources.parse(
                source.kind, raw, activity, source_id=source.id, config=source.config
            )
        except Exception:  # noqa: BLE001 — one dead feed must not lose the digest
            unavailable.append(activity.name)
            continue

        for event in result.events:
            if not starts <= event.starts_at <= ends:
                continue
            rendered = render(event, activity, children, settings)
            entries.append(
                Entry(
                    starts_at=event.starts_at,
                    tz=event.tz,
                    title=rendered.title,
                    venue=(event.venue.name if event.venue else None),
                    is_game=event.is_game,
                )
            )

    return Digest(entries=entries, unavailable=unavailable, starts=starts, ends=ends)
