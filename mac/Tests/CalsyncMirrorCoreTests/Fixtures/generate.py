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

ROGUES = Activity(
    id="rogues", child_id="luke", name="Rogues", official_name="Naboo Rogues U11",
    sport="soccer", emoji="⚽️", tz=TZ, home_venue="Theed Park",
)
LUKE = Child(id="luke", name="Luke", initial="Lu", birth_order=1)

THEED = Venue(
    raw="Theed Park #2 1009 Theed Rd, Naboo NX 40114",
    name="Theed Park",
    address="1009 Theed Rd, Naboo NX 40114",
    field="#2",
)
ALDER_REACH = Venue(
    raw="Jedi Temple Memorial Park 7160 Senate Ln, Coruscant, NX 40219",
    name="Jedi Temple Memorial Park",
    address="7160 Senate Ln, Coruscant, NX 40219",
)

# 14:00 EDT, carried as the UTC instant an adapter would hand downstream.
timed = Event(
    uid="360Player-event-7716716",
    activity_id="rogues",
    starts_at=datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc),
    ends_at=datetime(2026, 9, 12, 19, 30, tzinfo=timezone.utc),
    is_game=True,
    tz=TZ,
    opponent="Mos Eisley FC",
    home=True,
    venue=THEED,
    url="https://app.360player.com/event/7716716",
    source_id="p360-rogues",
    content_hash="abc123def456",
)

# A day with no time published: local midnight in the activity's zone, which is
# what `Event.all_day` means and what makes `to_vevent` emit VALUE=DATE.
allday = Event(
    uid="360Player-event-7716999",
    activity_id="rogues",
    starts_at=datetime(2026, 10, 3, tzinfo=ZoneInfo(TZ)),
    ends_at=datetime(2026, 10, 4, tzinfo=ZoneInfo(TZ)),
    is_game=True,
    tz=TZ,
    venue=ALDER_REACH,
    detail="Semifinal Games",
    all_day=True,
    source_id="p360-rogues",
    content_hash="999zzz",
)

with tempfile.TemporaryDirectory() as tmp:
    settings = Settings.load(open_db(pathlib.Path(tmp) / "fixture.db"))
    rendered = [
        render(timed, ROGUES, [LUKE], settings, alarm_minutes=90),
        render(allday, ROGUES, [LUKE], settings),
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
