# calsync API

The API is the **only** writer to the calendar store. Hermes, the email worker,
ICS pollers, scrapers, and the web UI all go through it. Nothing writes to
Radicale directly.

Base: `http://calsync.<tailnet>.ts.net/v1`
Auth: `Authorization: Bearer <token>`, scoped per client.

| Scope | documents | proposals | read events | amend | approve |
|---|---|---|---|---|---|
| `agent` (Hermes) | create | create | yes | via task token | — |
| `poller` | create | create | — | — | auto |
| `ui` (you) | create | read | yes | yes | yes |

`agent` deliberately cannot approve its own proposals. The review gate is
structural, not conventional. Reads are standing (low risk, and Hermes needs
them for the paste flow); amendment writes come from a per-task token scoped to
named UUIDs.

### Configuration is not in this API

Children, sports, venues, venue aliases, activities, sources and settings are
**edited directly in SQLite** by the web UI, in the same process. They do not go
through this API and no scope above covers them.

The "only writer" rule is about the **calendar store** — nothing but calsync
writes to Radicale. Configuration tables are not the calendar store, and the gate
that motivates the rule does not apply to them: it exists so an *agent* cannot
approve its own proposals, not to stand between you and your own kid's team name.
This is a single-household tool on a private tailnet with one human operator, so
an HTTP layer over config CRUD would add a component and a token to manage
without adding a control.

**What this does not license.** The boundary is agent-vs-human, not
inside-vs-outside the house:

- Hermes and the pollers still create *proposals*; they never write events, and
  they never approve. Being on a private network changes nothing about that —
  the risk is an agent acting on a bad parse, not a stranger on the internet.
- Nothing but calsync writes to Radicale, still.
- If a future component that is not the operator's own browser needs to change
  configuration, it needs an API and this decision gets revisited.

**Consequence:** the poller and the UI are then two processes on one SQLite file.
The schema runs in WAL mode, which permits concurrent readers alongside one
writer, but a second writer fails immediately unless a busy timeout is set —
`db.connect()` sets one. Keep any UI write transaction short; do not hold one
open across a request.

### Hermes reads through the API, not CalDAV

Giving Hermes a read-only Radicale account looks equivalent and isn't:

1. **The calendar holds renders, not data.** `SUMMARY` is generated from
   structured fields ([NAMING.md](NAMING.md)). An agent reading CalDAV would
   parse `Nadia 🏊 Distance Set` back into child and activity — reverse
   -engineering a string we just generated, and re-breaking every time the
   convention changes.
2. **The disambiguation context isn't in the VEVENT.** Activity IDs, venue
   aliases, confidence, source tiers, and which source currently owns a field
   all live in SQLite.
3. **One fewer credential and network path** to manage or misconfigure.
4. **CalDAV shows published state**, which can lag or lead the database while a
   sync is in flight. The API presents one coherent view.

A read-only Radicale account is still worth creating — for you, for Thunderbird
or DAVx5, for debugging. Just not as an agent's data source.

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
    "child_hint": "Nadia",
    "activity_hint": "U12 Soccer - Brookvale FC",
    "title": "vs. Northside United",
    "kind": "game",
    "starts_at": "2026-09-13T14:00:00-04:00",
    "tz": "America/New_York",
    "ends_at": "2026-09-13T15:30:00-04:00",
    "all_day": false,
    "venue_raw": "Brookvale Park Field 4",
    "venue_address": "1200 Brookvale Dr, Springfield",
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
  "collection": "nadia/soccer-fall-2026",
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
      "detail": "No child matches 'Nadia R.'",
      "candidates": ["Nadia", "Noah"] }
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
GET    /v1/events?from=&to=&child=&activity=      # built
GET    /v1/events/{uid}                           # built
PATCH  /v1/events/{uid}              # bumps SEQUENCE
DELETE /v1/events/{uid}              # STATUS:CANCELLED tombstone, not a purge
```

The two reads are **the only part of this document that exists**. `calsync api`
serves them on port 8731, and the compose stack serves them at `/v1` on its one
published port, where the paths in this document and in `GET /v1` are correct
as written (`docs/deployment/proxy.md`); everything else here — documents, proposals, review,
tasks, amendments — is a design contract with no code behind it, because none
of its consumers exist yet. Cancellation still writes a tombstone rather than
purging, so a deletion propagates instead of leaving a stale event on every
device that already synced it; that is what `cancelled: true` on a read means.

```json
{
  "from": "2026-08-11T00:00:00+00:00",
  "to": "2026-09-01T00:00:00+00:00",
  "count": 1,
  "events": [ { "…": "as in `events/query` below" } ],
  "sources": [
    { "id": "p360-jesse-vanguard", "enabled": true,
      "last_success_at": "2026-08-18T02:08:25+00:00",
      "last_error": null, "stale": false }
  ]
}
```

**Bounds.** `from` defaults to now, `to` to fourteen days out, and a span wider
than the sync window is refused — nothing is written outside it, so a wider ask
is a misunderstanding rather than a big query. Content is pruned below
`sync_window_back_days`, and a request reaching under that floor is clamped and
told so via `retained_from`, never answered with a silently shorter list.

**`sources` is the staleness answer.** A stored copy lags its feed by at most
one poll, exactly as the calendar always has — fine, as long as it is legible.
A source whose last poll failed, or which has gone quiet for more than a couple
of its own intervals, is named here rather than having its events served as
though they were current. Same rule `digest.py` follows when it names feeds it
could not read: quietly omitting one reads as "nothing on".

**Auth is a bearer token and nothing else.** No cookies, so no CSRF surface and
no `Sec-Fetch-Site` check — the console needs one precisely because it has no
token, and this has the opposite arrangement. The token lives in the secret
store under `api_token_ref`; `calsync api` refuses to start without it.

---

## Tasks (calsync → Hermes)

calsync dispatches work it can't resolve — venue strings, ambiguous names —
as tasks posted to the Matrix room ([MATRIX.md](MATRIX.md)). Hermes answers on
the API, not in chat.

```http
GET  /v1                         # built — the machine copy of this document
GET  /v1/tasks?state=open
POST /v1/tasks/{id}/result       # built
```

**`GET /v1` is the contract an agent should read before operating.** It is
generated from `api/contract.py` and held against the running app's route table
by a test, in both directions, so it cannot describe an endpoint that does not
exist and an endpoint cannot exist undescribed. This document is the human copy
and will drift from the code; that endpoint cannot. It also lists what is
specified here and not built, and anything on that list answers **501 with the
reason** rather than a bare 404 — "not yet, and here is why" is a different
answer from "you have the URL wrong", and an agent given the second will retry
variations of it.

`POST /v1/tasks/{id}/result` exists. It takes `{"answer": {...},
"answered_by": "...", "rationale": "..."}` and **stores it without applying
it** — the response says `"applied": false` for exactly that reason. A human
approves in the console, which is the only path to the code that writes an
alias, a vocabulary word or a venue row.

It uses the standing token rather than the scoped per-task token described
below. That token exists in this document because an unreviewed answer could
otherwise be turned into a write against arbitrary events; with a human
approval in front of every application, a hijacked answer produces a queue item
and nothing else. **Scoped tokens become necessary the moment anything applies
an answer automatically**, and that is the condition to re-read this under.

## Amendments

Mutating already-published events from a pasted coach message. Higher risk than
a proposal — these events are already on other people's phones.

```http
POST /v1/events/query            # bounded selector → UUIDs + current values
POST /v1/amendments
POST /v1/amendments/{id}/undo
```

### Query, then amend

`events/query` takes **the same selector object** as `amendments`. Query it,
check the matches, then pass the identical selector (or the returned UUIDs) to
amend. No translation step between the two calls is where agent errors would
otherwise creep in.

```json
POST /v1/events/query
{ "selector": { "activity": "swim-practice", "child": "nadia",
                "from": "2026-09-14", "to": "2026-09-20" } }
```

```json
{
  "query_id": "q_01J9XR…",
  "count": 4,
  "events": [
    {
      "uuid": "3f9c1a2e-…@calsync",
      "child":    { "id": "nadia", "name": "Nadia" },
      "activity": { "id": "swim-practice", "name": "Swim — Brookvale Aquatics",
                    "sport": "swim" },
      "kind": "practice",
      "is_game": false,
      "starts_at": "2026-09-14T17:30:00-04:00",
      "tz": "America/New_York",
      "venue": { "id": 12, "canonical_name": "Brookvale Aquatic Center",
                 "raw": "Aquatic Ctr Main", "pin_confirmed": true },
      "summary_rendered": "Nadia 🏊",
      "resolution": {
        "venue": { "source_id": "swim-fall-pdf", "tier": 4,
                   "observed_at": "2026-08-09T11:04:00-04:00" }
      }
    }
  ]
}
```

Three things this gives an agent that a calendar read cannot:

- **Structured identity.** `child.id` and `activity.id` as fields, not parsed
  out of a display string.
- **`resolution` per field** — which source currently owns this value, its tier
  and timestamp. Hermes can see that the venue is held by a tier-4 PDF and a
  tier-1 relay will therefore win, instead of submitting a no-op amendment.
- **`query_id`** — pass it to `POST /v1/amendments` and the call fails if the
  matched set changed since the query. Cheap protection against someone adding
  a swim practice between the two calls.

`summary_rendered` is derived output for display. **Never parse it** — it's a
render of the fields above ([NAMING.md](NAMING.md)) and it changes whenever the
naming convention does.

Selectors default to a 14-day window and are always bounded.

```json
{
  "selector": { "activity": "swim-practice", "child": "nadia",
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
  or description only; the API composes `Nadia ⚽️ vs Northside`.
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

---

## What calsync remembers

`GET /v1/events` and `GET /v1/events/{uid}` are the half of this API that
motivates it — §"Hermes reads through the API, not CalDAV" argues at length that
an agent must not reverse-engineer event data out of rendered calendar entries.
For a long time it could not be built, because **calsync stored no event data**:
`event_state` held placement and a hash and nothing else, which is why
`digest.py` re-parses every feed to answer "what's on tomorrow".

`event_content` is the answer, and the shape of it is four decisions rather than
a schema chore.

### 1. It is a receipt, not a cache

The obvious failure of a second copy is "the API says 7pm, the calendar says
8pm". That failure is entirely a consequence of *when* the copy is written.
Write it at parse time and it is a cache of the feed, free to disagree with what
was actually published. Write it in the same call that records placement — after
the target has accepted the write, behind `sync.py`'s existing ordering barrier —
and disagreement is structurally impossible. One `Event`, one code path, one
successful write, two rows.

What remains is that the row lags the *feed* by up to one poll interval. That is
not a new failure mode: the calendar has always had exactly the same lag. The
read API inherits the staleness that already exists instead of inventing a
second one. A refused write leaves neither row, so an event that never reached
the calendar is never described as though it had.

### 2. It stores what the source said, not what was rendered

This is what makes [MATRIX.md §4](MATRIX.md) implementable later without
migrating anything written now.

```
calendar content = source layer  ⊕  amendment overlay
API response     = source layer  ⊕  amendment overlay
```

A poll writes only the source layer. It therefore *cannot* revert an amendment,
because it never touches the layer amendments live in — no trust-rank resolver,
no `superseded_by`, no expiry. Precedence falls out of which layer each writer
owns. Store rendered values here instead and every poll fights every amendment,
which is the silent revert §4 exists to prevent.

The overlay table is deliberately **not built**. Its only writer would be
`PATCH /v1/events/{uid}`, and an override layer with nothing writing to it is
unverifiable in the same way a review gate with nothing to review is. What is
built is the half whose semantics the other half depends on.

### 3. The title stays a render, and there are no coordinates

No `summary` column, ever. The display title is composed from these fields at
write time and re-composed at read time, which is what lets a naming convention
change re-render every event without re-fetching a feed. `summary_rendered` in a
read response is computed per request through the same `normalize/title.py` the
calendar goes through — which is why it must never be parsed.

No `lat`/`lon` either. `venues` already holds pins and is the only table that
should; a read response resolves `venue.id` and `pin_confirmed` from it live.

### 4. It is bounded to the window the calendar already keeps

This is children's names, locations and start times at rest in one more place,
on a box whose security posture is "it is only ever reached through a VPN". So
content ages out with `sync_window_back_days` — the same bound both sides of the
diff are already compared across, which is why a prune can never look like a
change. The `event_state` row survives: it is what recognises the event if it
ever comes back, and it holds nothing but a uid, a hash and a timestamp.

### The consequence for the sync loop

The diff's `content_hash` is taken over the **raw feed component**, before the
venue table is consulted. So stored content is compared independently of it,
exactly as placement already is — otherwise teaching an alias or confirming an
address would change what an event renders to while leaving its hash identical,
and the correction would never reach anyone's phone. Reported as `refreshed`
rather than `updated`, because `updated` means the feed changed and these are
precisely the cases where it did not. Rows written before the table existed are
just one more difference, so a stable season backfills itself on the next poll.
