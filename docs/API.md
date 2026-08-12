# calsync API

The API is the **only** writer to the calendar store. Hermes, the email worker,
ICS pollers, scrapers, and the web UI all go through it. Nothing writes to
Radicale directly.

Base: `http://calsync.<tailnet>.ts.net/v1`
Auth: `Authorization: Bearer <token>`, scoped per client.

| Scope | documents | proposals | approve | events (direct) |
|---|---|---|---|---|
| `agent` (Hermes) | create | create | — | — |
| `poller` | create | create | auto | — |
| `ui` (you) | create | read | yes | yes |

`agent` deliberately cannot approve its own proposals or write events. The
review gate is structural, not conventional.

---

## Documents

Upload the source artifact first; proposals reference it.

```http
POST /v1/documents
Content-Type: multipart/form-data

file=@fall-schedule.pdf
source_hint=hermes-local
received_at=2026-08-12T14:02:00-04:00
```

```json
{
  "document_id": "doc_01J8X...",
  "sha256": "9f2b...",
  "mimetype": "application/pdf",
  "pages": 3,
  "duplicate_of": null
}
```

If the same bytes were uploaded before, `duplicate_of` names the earlier
document and no new blob is stored. Hermes can stop there if it wants to skip
re-extraction.

Accepts PDFs, images (whiteboard photos, screenshots), `.ics`, `.eml`, and
plain text.

---

## Proposals

An extracted candidate event. The API decides what happens to it.

```http
POST /v1/proposals
Idempotency-Key: sha256:9f2b...:e3
```

```json
{
  "document_id": "doc_01J8X...",
  "extractor": "hermes/1.4",
  "prompt_version": "sched-extract-v7",
  "event": {
    "child_hint": "Nora",
    "activity_hint": "U12 Soccer - Riverside FC",
    "title": "vs. Northside United",
    "kind": "game",
    "starts_at": "2026-09-13T14:00:00-04:00",
    "tz": "America/New_York",
    "ends_at": "2026-09-13T15:30:00-04:00",
    "all_day": false,
    "venue_raw": "Riverside Park Field 4",
    "venue_address": "1200 Riverside Dr, Springfield",
    "status": "confirmed",
    "notes": "Arrive 45 min early for warmups"
  },
  "confidence": {
    "overall": 0.91,
    "starts_at": 0.97,
    "venue": 0.64,
    "child": 0.88
  },
  "raw_text": "Sat 9/13  2:00p  vs Northside United  RP Field 4  (arrive 1:15)",
  "source_ref": { "page": 2, "bbox": [72, 344, 520, 366] }
}
```

Response:

```json
{
  "proposal_id": "prop_01J8X...",
  "state": "accepted",
  "event_uid": "3f9c1a2e-...@calsync",
  "collection": "nora/soccer-fall-2026",
  "action": "created"
}
```

### States

| `state` | Meaning |
|---|---|
| `accepted` | Written to CalDAV. `action` is `created`, `updated`, or `unchanged`. |
| `pending_review` | Held in SQLite. Low confidence or unresolved conflict. |
| `duplicate` | Idempotency key or dedup match already known. No-op. |
| `rejected` | Failed validation. See `errors[]`. |

Auto-accept requires `confidence.overall ≥ 0.85`, a resolvable child and
activity, an explicit timezone, and no conflicting event. Otherwise
`pending_review` with `conflict_with_uid` set when relevant. Thresholds are
per-activity config, not hardcoded.

### Idempotency

`Idempotency-Key` is **required** on proposals. Derive it from
`sha256(document):<event_ordinal>` so re-running Hermes over the same PDF is a
no-op. Replaying a key returns the original result with `"replayed": true`.

### Validation errors

`422` with structured, correctable reasons:

```json
{
  "state": "rejected",
  "errors": [
    { "field": "starts_at", "code": "naive_datetime",
      "detail": "No UTC offset or tz. Send RFC3339 with offset plus IANA tz.",
      "got": "2026-09-13 14:00" },
    { "field": "child_hint", "code": "unresolved_entity",
      "detail": "No child matches 'Nora R.'",
      "candidates": ["Nora", "Noah"] }
  ]
}
```

Never guess at ambiguity. `6:00` with no meridiem, a bare `9/13` with no year,
or a `TBD` time should come back as low confidence with a flag — not a
plausible-looking wrong answer.

### Batching

`POST /v1/proposals/batch` takes `{"proposals": [...]}` for a season schedule.
Each element carries its own idempotency key and gets its own result; partial
success is normal and returns `207`.

---

## Review

```http
GET  /v1/proposals?state=pending_review
POST /v1/proposals/{id}/approve     {"edits": {...}}    # ui scope only
POST /v1/proposals/{id}/reject      {"reason": "..."}
```

Approve writes to CalDAV and records who decided and when. Edits made at
approval are stored as a correction against `(extractor, prompt_version)` —
that's your regression corpus for prompt changes.

---

## Events

```http
GET    /v1/events?from=&to=&child=&activity=
GET    /v1/events/{uid}              # includes provenance chain
PATCH  /v1/events/{uid}              # bumps SEQUENCE
DELETE /v1/events/{uid}              # STATUS:CANCELLED tombstone, not a purge
```

Cancellation writes a tombstone so the deletion propagates to subscribers.
Purging would leave a stale event on every device that already synced it.

---

## Tasks (calsync → Hermes)

calsync dispatches work it can't resolve — venue strings, ambiguous names —
as tasks posted to the Matrix room ([MATRIX.md](MATRIX.md)). Hermes answers on
the API, not in chat.

```http
GET  /v1/tasks?state=open
POST /v1/tasks/{id}/result       # scoped task token, 1h TTL
GET  /v1/schema/{name}           # JSON Schema, machine copy of this doc
```

The task token is scoped to that task and the event UUIDs it names. It is not
a standing credential and cannot write anything the task didn't cover.

## Amendments

Mutating already-published events from a pasted coach message. Higher risk than
a proposal — these events are already on other people's phones.

```http
POST /v1/events/query            # bounded selector → UUIDs + current values
POST /v1/amendments
POST /v1/amendments/{id}/undo
```

```json
{
  "selector": { "activity": "swim-practice", "child": "nora",
                "from": "2026-09-14", "to": "2026-09-20" },
  "patch":    { "venue_raw": "Aquatic Center East" },
  "rationale": "Coach message: main pool closed for maintenance",
  "source_document_id": "doc_01J9…"
}
```

- **Selectors must be date-bounded.** Open-ended is always rejected.
- **Blast radius gate:** 1–3 apply · 4–15 confirm in room · >15 refused.
- **Patches carry `venue_raw`, never coordinates.** Venue resolution stays
  server-side.
- Every amendment stores prior VEVENTs and is undoable.
- Amendments are recorded as **tier-1 field contributions**, not a separate
  override layer — see [MATRIX.md §4](MATRIX.md). Trust resolution keeps them
  from being clobbered by a stale poll or by re-running extraction over the
  original document.

## Sources & health

```http
GET  /v1/sources
GET  /v1/sources/{id}/health         # last_success_at, staleness, last_error
POST /v1/sources/{id}/poll           # manual trigger
```

---

## Notes for agent implementers

- **Don't send a display title.** `SUMMARY` is rendered server-side from
  structured fields ([NAMING.md](NAMING.md)). Send `title` as the raw opponent
  or description only; the API composes `Nora ⚽️ vs Northside · Riverside #4`.
  This is what lets the naming convention change without re-extracting anything.
- **Send `kind`, but don't agonize over it.** Routing is binary: `game` /
  `scrimmage` / `tournament` → Games, everything else → Practices. If you can't
  tell, send `"kind": "unknown"` — it defaults to Practices and is still
  delivered, just flagged for review. Never withhold an event over `kind`. An
  opponent in the source text is a strong `is_game` signal.
- **Send `venue_raw` verbatim.** Don't clean it up, don't expand abbreviations,
  and never emit `lat`/`lon` — venue resolution is a server-side pipeline with
  an alias cache and a real geocoder. `RP Field 4` is more useful to it than
  your best guess at a full address.
- Send RFC3339 with offset **and** the IANA `tz`. The offset alone loses the
  DST rule, which matters for a season crossing November.
- One proposal per event occurrence. Don't send RRULEs — the API expands and
  manages recurrence, because half of a "recurring" practice series gets
  individually moved anyway.
- Always include `raw_text`. It costs nothing and it's the only thing that
  makes a bad parse debuggable weeks later.
- Prefer re-uploading a document and getting `duplicate_of` over caching state
  locally. The server is the memory.
