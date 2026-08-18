"""What's on tomorrow, as a message.

Two implementations are wrong, for reasons the project has already written down:

- **Read it back from the calendar.** `docs/API.md` refuses this for Hermes and
  the refusal applies here too — the calendar holds *renders*, not data. Pulling
  "Patrick ⚽️ vs Strikers" back apart into a child and an opponent is
  reverse-engineering a string we generated ourselves, and it re-breaks every
  time the naming convention changes.
- **Re-parse the feeds.** This is what the digest used to do, when `event_state`
  held only hashes and placement and there was nothing else to read. It works,
  and it has a flaw that is easy to miss: it reports what the *feed* says, which
  is not always what is on the calendar. If the last poll was held by a guard,
  or failed, or has not run yet, the feed can carry a game nobody's phone has —
  and a message announcing it is confidently wrong in the one direction that
  gets somebody driving to a field.

So a digest reads the receipt: `event_content`, written after the target
accepted each write, re-rendered now through the same `normalize/title.py` the
calendar goes through. The message and the calendar are then one answer rather
than two that usually agree. It also costs no network at all, where the old
version fetched every feed once a day.

What it inherits is the receipt's own staleness — content is only as current as
the poll that wrote it. That is not hidden: a source whose last poll failed or
which has gone quiet is **named in the message**, on the same principle the old
version named a feed it could not read. Silently omitting a team reads as
"nothing on today", which is the one wrong answer a schedule message can give.

**It writes nothing.** Not to the database, not to the calendar, not a poll_runs
row. A digest is a read, and a read that quietly advanced sync state would make
"tell me what's on" a thing you had to think twice about running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import repo
from .normalize import title as title_norm
from .settings import Settings


def due(
    *,
    now_local: datetime,
    send_at: str,
    last_sent_on: date | None,
) -> bool:
    """Is today's digest due? Pure, because the loop around it is not testable.

    ``send_at`` is "HH:MM" local, and empty means never — a deployment that has
    not asked for a digest should not get one at midnight because a default
    looked harmless.

    Late is better than never: a poller started at 09:00 with a 07:00 digest
    still sends today's. The alternative is that restarting the container in the
    morning silently costs you the day's message, which is the kind of rule
    nobody remembers when wondering where it went.
    """
    if not send_at.strip():
        return False
    try:
        hour, minute = (int(part) for part in send_at.strip().split(":", 1))
    except ValueError:
        return False
    if last_sent_on == now_local.date():
        return False
    return (now_local.hour, now_local.minute) >= (hour, minute)


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
    #: Teams whose stored schedule may not be current — a feed that stopped
    #: answering, or one that has not been polled yet. Named rather than
    #: dropped, for the same reason a feed that could not be read used to be:
    #: a digest that silently omits a team reads as "nothing on today", which
    #: is the one wrong answer a schedule message can give.
    stale: list[str]
    starts: datetime
    ends: datetime

    @property
    def empty(self) -> bool:
        return not self.entries

    def text(self) -> str:
        day = self.starts.strftime("%A %-d %B")
        if self.empty and not self.stale:
            return f"**{day}** — nothing on."

        lines = [f"**{day}**"]
        lines += [f"- {e.line()}" for e in sorted(self.entries, key=lambda e: e.starts_at)]
        if self.stale:
            lines.append("")
            lines.append(
                "Not polled recently: " + ", ".join(sorted(self.stale))
                + " — so this may be out of date."
            )
        return "\n".join(lines)


def collect(conn, *, now: datetime, hours: int = 24) -> Digest:
    """Everything on the calendar that starts in the next ``hours``.

    Read out of `event_content`, which was written after each event reached the
    calendar — so this reports what is actually on somebody's phone, not what a
    feed currently says. No network at all.

    Cancelled events are left out: a tombstone is how a deletion propagates, not
    something that is on. Disabled sources are **not** left out, which is a
    change from re-parsing the feeds — pausing a source stops polling, it does
    not take its events off the calendar, and a family that still has a game on
    Saturday needs to be told about it. Retiring genuinely removes the events
    (`retire.py` cancels every one before it disables anything), so those are
    already excluded by being cancelled.
    """
    settings = Settings.load(conn)
    starts, ends = now, now + timedelta(hours=hours)

    entries: list[Entry] = []
    activities: dict[str, tuple] = {}

    for item in repo.stored_events(
        conn, start=starts.isoformat(), end=ends.isoformat()
    ):
        if item.cancelled:
            continue
        if item.activity_id not in activities:
            activity = repo.get_activity(conn, item.activity_id)
            activities[item.activity_id] = (
                activity,
                [repo.get_child(conn, activity.child_id)],
            )
        activity, children = activities[item.activity_id]
        entries.append(
            Entry(
                starts_at=item.event.starts_at,
                tz=item.event.tz,
                # Composed now, through the same code the calendar went through,
                # which is what makes the message agree with it rather than
                # approximate it.
                title=title_norm.render(item.event, activity, children, settings),
                venue=(item.event.venue.name if item.event.venue else None),
                is_game=item.event.is_game,
            )
        )

    # Named by team rather than by source id: this is a message to a person, and
    # "tr-comets-2026" is not what they call it.
    freshness = repo.source_freshness(conn, now=now)
    stale = [
        repo.get_activity(conn, source.activity_id).name
        for source in repo.list_sources(conn, enabled_only=True)
        if freshness[source.id].stale
    ]

    return Digest(entries=entries, stale=stale, starts=starts, ends=ends)
