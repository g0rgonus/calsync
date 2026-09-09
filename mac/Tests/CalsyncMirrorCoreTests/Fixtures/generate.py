#!/usr/bin/env python3
"""Regenerate games.ics from calsync's own pipeline.

Run from the repo root, with calsync's venv:

    .venv/bin/python mac/Tests/CalsyncMirrorCoreTests/Fixtures/generate.py

The point is that no part of this fixture is hand-written — and that includes
the **input**. An earlier version of this script ran the real serializer over a
`RenderedEvent` it built itself, with `tzinfo=ZoneInfo("America/New_York")`. The
adapters never produce that: they `.astimezone(timezone.utc)`, so `to_vevent`
emits `DTSTART:...Z` and never a `TZID`. The fixture therefore certified a
format Radicale has never served, the Swift parser was held against it, and
every timed event the mirror wrote went into EventKit with no timezone at all —
a floating event, which silently re-anchors whenever the Mac changes zone.

So the events below are built as `Event`s and put through `render()`, which is
the same call `sync.py` makes. The only hand-written values are the ones a feed
supplies.

The two events are chosen for the traps they carry, not for coverage:

  - a description long enough to fold, folding **mid-word** ("Field:" / " #2")
  - a 90-minute alarm, which icalendar writes as `-PT1H30M`, not `-PT90M`
  - `URL:` with a colon in the value
  - escaped commas in LOCATION
  - emoji and a middle dot, so the encoding path is exercised
  - an all-day event: `VALUE=DATE` with an **exclusive** DTEND
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from calsync.db import open_db  # noqa: E402
from calsync.models import Activity, Child, Event, Venue  # noqa: E402
from calsync.render import render  # noqa: E402
from calsync.settings import Settings  # noqa: E402
from calsync.targets.ics_file import to_ics  # noqa: E402

TZ = "America/New_York"

OTTERS = Activity(
    id="otters", child_id="jesse", name="Otters", official_name="Marbury Otters U11",
    sport="soccer", emoji="⚽️", tz=TZ, home_venue="Thistledown Park",
)
JESSE = Child(id="jesse", name="Jesse", initial="J", birth_order=1)

THISTLEDOWN = Venue(
    raw="Thistledown Park #2 1009 Thistledown Rd, Marbury NX 40114",
    name="Thistledown Park",
    address="1009 Thistledown Rd, Marbury NX 40114",
    field="#2",
)
ALDER_REACH = Venue(
    raw="Alder Reach Memorial Park 7160 Kestrel Ln, Fenwick, NX 40219",
    name="Alder Reach Memorial Park",
    address="7160 Kestrel Ln, Fenwick, NX 40219",
)

# 14:00 EDT, carried as the UTC instant an adapter would hand downstream.
timed = Event(
    uid="360Player-event-4716716",
    activity_id="otters",
    starts_at=datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc),
    ends_at=datetime(2026, 9, 12, 19, 30, tzinfo=timezone.utc),
    is_game=True,
    tz=TZ,
    opponent="Harbour FC",
    home=True,
    venue=THISTLEDOWN,
    url="https://app.360player.com/event/4716716",
    source_id="p360-otters",
    content_hash="abc123def456",
)

# A day with no time published: local midnight in the activity's zone, which is
# what `Event.all_day` means and what makes `to_vevent` emit VALUE=DATE.
allday = Event(
    uid="360Player-event-4716999",
    activity_id="otters",
    starts_at=datetime(2026, 10, 3, tzinfo=ZoneInfo(TZ)),
    ends_at=datetime(2026, 10, 4, tzinfo=ZoneInfo(TZ)),
    is_game=True,
    tz=TZ,
    venue=ALDER_REACH,
    detail="Semifinal Games",
    all_day=True,
    source_id="p360-otters",
    content_hash="999zzz",
)

with tempfile.TemporaryDirectory() as tmp:
    settings = Settings.load(open_db(pathlib.Path(tmp) / "fixture.db"))
    rendered = [
        render(timed, OTTERS, [JESSE], settings, alarm_minutes=90),
        render(allday, OTTERS, [JESSE], settings),
    ]

# One VCALENDAR carrying both VEVENTs — which is what Radicale serves on a GET
# of the collection, and therefore what the tool actually reads.
parts = [to_ics(e).decode() for e in rendered]
header = parts[0].split("BEGIN:VEVENT")[0]
bodies = [
    "BEGIN:VEVENT" + p.split("BEGIN:VEVENT", 1)[1].rsplit("END:VCALENDAR", 1)[0]
    for p in parts
]
out = pathlib.Path(__file__).with_name("games.ics")
out.write_text(header + "".join(bodies) + "END:VCALENDAR\r\n")
print(f"wrote {out}")
