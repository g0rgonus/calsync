# calsync

Family calendar sync platform: pull kids' schedules out of youth-sports apps,
PDFs, and email; normalize them into one source of truth; push them into
iCloud calendars.

Status: **planning**. See [docs/PLAN.md](docs/PLAN.md) for the architecture,
gap analysis, and phased roadmap.

## Shape

```
sources → ingest adapters → raw documents → AI/rule extraction
       → canonical events (versioned, deduped) → CalDAV push to iCloud
                                              → tokenized ICS feeds
       → web UI (feed management, review queue, source health)
```

## Next step

Phase 0: survey each real source for an API or ICS export before writing any
ingestion code.
