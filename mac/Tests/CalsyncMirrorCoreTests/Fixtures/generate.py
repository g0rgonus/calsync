#!/usr/bin/env python3
"""Regenerate games.ics from calsync's own serializer.

Run from the repo root, with calsync's venv:

    .venv/bin/python mac/Tests/CalsyncMirrorCoreTests/Fixtures/generate.py

The point is that no part of this fixture is hand-written. The Swift parser is
held against what `targets/ics_file.py:to_vevent` actually emits, so a change to
calsync's serialization shows up as a failing Swift test rather than as events
quietly going missing from a phone.

The two events are chosen for the traps they carry, not for coverage:

  - a description long enough to fold, folding **mid-word** ("Field:" / " #2")
  - a 90-minute alarm, which icalendar writes as `-PT1H30M`, not `-PT90M`
  - `URL:` with a colon in the value
  - escaped commas in LOCATION
  - emoji and a middle dot, so the encoding path is exercised
  - an all-day event: `VALUE=DATE` with an **exclusive** DTEND
"""

from datetime import datetime
from zoneinfo import ZoneInfo
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from calsync.render import RenderedEvent  # noqa: E402
from calsync.targets.ics_file import to_ics  # noqa: E402

tz = ZoneInfo("America/New_York")

timed = RenderedEvent(
    uid="360Player-event-4716716",
    collection="games",
    title="Jesse ⚽️ vs Harbour FC",
    starts_at=datetime(2026, 9, 12, 14, 0, tzinfo=tz),
    ends_at=datetime(2026, 9, 12, 15, 30, tzinfo=tz),
    tz="America/New_York",
    body=(
        "Soccer · Otters (Marbury Otters U11)\nStart 14:00 EDT\nField: #2\n"
        "1009 Thistledown Rd, Marbury NX 40114\nSource: p360-otters"
    ),
    location_text="Thistledown Park, 1009 Thistledown Rd, Marbury NX 40114",
    venue_name="Thistledown Park",
    url="https://app.360player.com/event/4716716",
    alarm_minutes=90,
    is_game=True,
    provenance={"uid": "360Player-event-4716716", "source": "p360-otters",
                "activity": "otters", "hash": "abc123def456"},
)

allday = RenderedEvent(
    uid="360Player-event-4716999",
    collection="games",
    title="Jesse ⚽️ Semifinal Games",
    starts_at=datetime(2026, 10, 3, 0, 0, tzinfo=tz),
    ends_at=datetime(2026, 10, 4, 0, 0, tzinfo=tz),
    tz="America/New_York",
    all_day=True,
    body="Soccer · Otters\nAll day — no time published yet\nSource: p360-otters",
    location_text="Alder Reach Memorial Park, 7160 Kestrel Ln, Fenwick, NX 40219",
    alarm_minutes=None,
    is_game=True,
    provenance={"uid": "360Player-event-4716999", "source": "p360-otters",
                "activity": "otters", "hash": "999zzz"},
)

# One VCALENDAR carrying both VEVENTs — which is what Radicale serves on a GET
# of the collection, and therefore what the tool actually reads.
parts = [to_ics(e).decode() for e in (timed, allday)]
header = parts[0].split("BEGIN:VEVENT")[0]
bodies = [
    "BEGIN:VEVENT" + p.split("BEGIN:VEVENT", 1)[1].rsplit("END:VCALENDAR", 1)[0]
    for p in parts
]
out = pathlib.Path(__file__).with_name("games.ics")
out.write_text(header + "".join(bodies) + "END:VCALENDAR\r\n")
print(f"wrote {out}")
