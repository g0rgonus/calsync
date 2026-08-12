# Matrix room: calsync ↔ Hermes ↔ you

A private room on the home Matrix server with three members: you,
`@hermes:home`, and `@calsync_bot:home`.

The room is a **task channel and an audit transcript**. It is not a result
channel and not a source of truth — Hermes answers by calling the API, and any
chat reply it posts is a human-readable courtesy. If a chat message and the
database disagree, the database is right.

`calsync_bot` is an API client like everything else. It holds no logic and
never writes to CalDAV. Chat in, API calls out.

---

## 1. Identity and authority

Authority comes from the **Matrix sender ID**, never from message content.

| MXID | Scope | Can |
|---|---|---|
| `@dan:home` | `ui` | approve, amend, query, upload |
| `@hermes:home` | `agent` | propose, resolve tasks, query — **never approve** |
| anyone else | none | ignored entirely |

Hard rules:

- A message saying "approved by Dan" is not approval. Only a message *from*
  Dan's MXID is.
- The bot acts only on messages that come from a mapped MXID **and** match a
  command grammar (`!cal …`, a bot mention, or a structured task reply).
  Free-form prose — especially forwarded or pasted content — is data, never a
  command.
- New room members get no scope by default. Log a membership change; don't
  silently extend trust.
- The bot ignores its own messages and never re-ingests Hermes's.

### Why the blast radius is already small

The room will contain pasted coach emails and forwarded screenshots — untrusted
text, and a live prompt-injection surface. The existing scope split contains it:
Hermes can only *propose*, so a successful injection against Hermes produces a
pending review item, not a calendar entry. Injection cannot escalate to
approval without a message from your MXID.

Amendments (§3) are the exception — they mutate live events — which is why they
get their own blast-radius limits and mandatory undo.

---

## 2. calsync → Hermes: structured task dispatch

The primary flow. calsync hits something it can't resolve — a venue string, an
ambiguous team name, an unmappable child — and posts a work order naming the
event UUID and the context needed. Hermes acts **on the API**; the chat is
where the request is legible.

```json
{
  "task_id": "task_01J9XQ…",
  "type": "normalize_venue",
  "event_uuid": "3f9c1a2e-…@calsync",
  "context": {
    "venue_raw": "AC East pool",
    "activity": "Swim — Riverside Aquatics",
    "starts_at": "2026-09-14T17:30:00-04:00",
    "known_aliases": ["Aquatic Center", "AC", "Aquatic Ctr Main"]
  },
  "respond_via": {
    "endpoint": "POST /v1/tasks/task_01J9XQ…/result",
    "schema": "/v1/schema/venue_resolution",
    "token": "<scoped, 1h TTL>"
  }
}
```

Task types: `normalize_venue`, `normalize_name`, `resolve_child`,
`resolve_activity`, `classify_kind`.

### Capability-scoped task tokens

The `token` is scoped to **this task and the event UUIDs it names**, with a
short TTL — not Hermes's standing credential. A task about one swim practice
cannot be turned into a write against next month's games.

This matters precisely because the room is a semi-public surface: anything
pasted in is untrusted, and per-task scoping means a hijacked task can only
damage what the task already covered.

### Post the contract, don't teach it in chat

Don't rely on Hermes inferring the API from scrollback — that's unbounded and
unversioned. Serve `GET /v1/schema/*` as JSON Schema, pin a room message
pointing at it, and let Hermes fetch the current contract. [API.md](API.md) is
the human copy; the endpoint is the machine copy, and they version together.

### Keep the room readable

A season PDF generating 40 proposals must post **one threaded summary**, not 40
messages. Use Matrix threads per document or per task batch, and put approvals
on reactions (👍/👎) rather than command syntax.

---

## 3. You → chat → Hermes → API: amendments

> "Coach says the pool's closed, swim practice is at Aquatic Center East all
> next week."

Pasted text against **already-published events**. Higher risk than a proposal,
because it mutates what's already on four people's phones.

```
1. Paste.    Bot stores the message as a raw_document
             (source: matrix, sender MXID, room, event_id). Full provenance.
2. Select.   Hermes calls POST /v1/events/query with a bounded selector
             → matched UUIDs + current values.
3. Amend.    Hermes calls POST /v1/amendments with the UUIDs and the patch.
4. Gate.     calsync applies or asks, by blast radius (below).
5. Record.   Prior VEVENTs stored; amendment_id is undoable.
```

```json
POST /v1/amendments
{
  "selector": { "activity": "swim-practice", "child": "nora",
                "from": "2026-09-14", "to": "2026-09-20" },
  "patch":    { "venue_raw": "Aquatic Center East" },
  "rationale": "Coach message: main pool closed for maintenance",
  "source_document_id": "doc_01J9…",
  "sticky_until": "2026-09-21"
}
```

The patch carries `venue_raw`, not coordinates — it goes through the normal
venue pipeline (alias → geocoder → confirm), same as any other venue string.

### Blast radius gate

| Events matched | Behavior |
|---|---|
| 1–3 | Apply, post what changed |
| 4–15 | Post the list, apply on your 👍 |
| >15 | Refuse. Too broad for a chat message — use the web UI |

Selectors must be date-bounded. An open-ended selector is always refused: "all
swim practices" with no end date is how one ambiguous sentence rewrites a
season.

### Undo is mandatory

This path is an LLM interpreting pasted prose into a mutation of live events.
Store the prior VEVENT for every touched event and support
`!cal undo <amendment_id>`. Not optional.

---

## 4. The stickiness trap

**This is the one that will bite you.**

Hermes amends swim practice to Aquatic Center East. Fifteen minutes later the
ICS poller runs, the upstream feed still says the main pool, and the sync
faithfully reverts everything. Then someone re-pastes, and it flaps.

Amendments must be recorded as **overrides that survive subsequent source
syncs**:

```
overrides(id, event_uuid, field, value, source_document_id,
          applied_at, expires_at, cleared_at, amendment_id)
```

Sync order becomes: **apply source data → apply live overrides on top → diff →
push.** An override wins over the feed until one of:

- `expires_at` passes (default: end of the amendment's date range — an override
  should not outlive the week it was about),
- the source itself changes to match, at which point the override is redundant
  and gets cleared automatically,
- you clear it explicitly.

Post a note to the room when an override expires or gets superseded, so a
silently-reverted venue is never a surprise on a Saturday morning.

---

## 5. Ingesting from the room

Dropping a PDF or a photo of a whiteboard into the room is the lowest-friction
capture path there is — better than email for anything a coach texts you.

- Any file from an authorized MXID → `POST /v1/documents` → extraction →
  proposals, replied in a thread.
- Re-dropping the same file hashes to `duplicate_of` and replies "already
  ingested, 12 events, no changes" rather than duplicating.
- Pasted text is a document too, which is what makes §3 work.

Read-only queries are safe and useful for both members: `!cal today`,
`!cal week`, `!cal next nora`, `!cal sources`, `!cal pending`.

---

## 6. Operational notes

**Encryption.** Bots in E2EE rooms need device verification and key management,
and that's the single most common reason a Matrix bot is miserable to run. On a
homeserver on your own network, an unencrypted private room is the pragmatic
call. If the homeserver federates or is externally reachable, budget the E2EE
work instead — this room carries kids' schedules and locations.

**Runtime.** Another process alongside the API and Radicale. `matrix-nio`
(Python) or `matrix-bot-sdk` (TS).

**The room is also the alerting channel** (PLAN.md §D1): source went stale,
scraper broke, credentials need re-auth, N items pending review. It's already
the place you'll be looking.
