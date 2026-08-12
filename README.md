# calsync

Family calendar sync platform: pull kids' schedules out of youth-sports apps,
PDFs, and email; normalize them into one source of truth; push them into
iCloud calendars.

Status: **planning**. See [docs/PLAN.md](docs/PLAN.md) for the architecture,
gap analysis, and phased roadmap, and [docs/API.md](docs/API.md) for the
write API that agents and pollers code against.

## Shape

```
Hermes (PDF/photo) ─┐
email worker       ─┼─→ calsync API ─→ Radicale (CalDAV)  ─→ phone, direct
ICS pollers        ─┤   (only writer)  SQLite (raw docs,  ─→ iCloud (later)
web UI / manual    ─┘        │          proposals, sync)  ─→ ICS feeds
                       review queue
```

The API is the only writer. Agents submit *proposals*; the API decides what
reaches the calendar.

## Next step

Phase 0: survey each real source for an API or ICS export before writing any
ingestion code.
