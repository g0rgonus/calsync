# calsync

Family calendar sync platform: pull kids' schedules out of youth-sports apps,
PDFs, and email; normalize them into one source of truth; push them into
iCloud calendars.

Status: **planning**. See [docs/PLAN.md](docs/PLAN.md) for the architecture,
gap analysis, and phased roadmap, and [docs/API.md](docs/API.md) for the
write API that agents and pollers code against. [docs/NAMING.md](docs/NAMING.md)
covers event title and location conventions, and
[docs/MATCHING.md](docs/MATCHING.md) covers dedup and adopting the hand-created
events already in the shared calendars.
[docs/MATRIX.md](docs/MATRIX.md) covers the Matrix room where calsync, Hermes,
and you interact. [docs/sources/](docs/sources/) holds per-source adapter
notes from the Phase 0 survey, [docs/deployment/](docs/deployment/) holds
infrastructure requirements, and [docs/config.example.yaml](docs/config.example.yaml)
shows a real feed wired up end to end.

## Shape

```
Hermes (PDF/photo) ─┐                                    ┌─→ iCloud: Games
email worker       ─┼─→ calsync API ─→ Radicale (CalDAV) ─┼─→ iCloud: Practices
ICS pollers        ─┤   (only writer)  SQLite (raw docs, ─┤   (shared w/ family)
Matrix bot / paste ─┤                  proposals, sync)  └─→ ICS feeds
web UI (feeds)     ─┘        │
                       review queue
```

The API is the only writer. Agents submit *proposals*; the API decides what
reaches the calendar. calsync manages sourced events only — hand-created
appointments are never touched.

Matrix room for the daily loop (capture, approve, amend). Web UI for setup
(add a feed, bind it to a kid and sport).

## Next step

Phase 0: survey each real source for an API or ICS export before writing any
ingestion code.
