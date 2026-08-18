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

For document-sourced activities this is not a convenience path, it's *the*
change channel. Swim's season comes from a PDF and every subsequent change
arrives by email or text — there is no feed that will ever carry the new pool.
If this path doesn't work well, swim schedules are wrong for the rest of the
season.

```
1. Paste.    Bot stores the message as a raw_document
             (source: matrix, sender MXID, room, event_id). Full provenance.
2. Select.   Hermes calls POST /v1/events/query with a bounded selector
             → UUIDs, structured fields, and which source owns each value.
3. Amend.    Hermes calls POST /v1/amendments with the same selector plus
             the query_id, so the call fails if the set changed underneath.
4. Gate.     calsync applies or asks, by blast radius (below).
5. Record.   Prior VEVENTs stored; amendment_id is undoable.
```

Step 2 is how Hermes gets UUIDs — through the API, not by reading CalDAV.
Query and amend take the same selector object, so there's no translation step
between them. See [API.md](API.md) for why the calendar is the wrong read
source for an agent.

```json
POST /v1/amendments
{
  "selector": { "activity": "swim-practice", "child": "nora",
                "from": "2026-09-14", "to": "2026-09-20" },
  "patch":    { "venue_raw": "Aquatic Center East" },
  "rationale": "Coach message: main pool closed for maintenance",
  "source_document_id": "doc_01J9…"
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

## 4. Amendments are a high-trust source, not an override layer

An earlier draft of this doc specced an `overrides` table with expiry, on the
assumption a poller would keep re-asserting the old value. That's wrong for
swim: swim is a **PDF ingest** whose changes arrive by email or text. The
document was read once and nothing re-asserts it, so there's no poll to fight
and nothing to expire.

Model an amendment as what it is — **a contribution from the highest-trust
source** — and the existing trust ranking ([PLAN.md §B3](PLAN.md)) resolves
everything with no new machinery:

```
field value := highest trust tier wins;
               within a tier, most recent wins
```

A coach message relayed by a human is tier 1. A poller re-asserting a stale
venue is tier 3 and loses. A re-extraction of the original PDF carries that
*document's* timestamp, not today's, so replaying extraction with a better
prompt can't clobber a newer amendment.

That last point matters more than it looks: re-running extraction over stored
raw documents is a first-class feature ([PLAN.md §A4](PLAN.md)), and a naive
implementation would silently wipe every amendment on the next replay.

### The one case that still needs a decision

A **revised PDF** arrives in October, lower trust tier than the amendment but
genuinely newer information. Rule:

> When a lower-tier source produces a value that is both newer than the current
> winner and different from it, **do not silently pick either** — flag it to
> review and post it to the room.

Same principle as everywhere else: automatic when unambiguous, human when not.

### What replaces expiry

Nothing. The date-bounded selector already scoped the amendment to specific
event UUIDs — "the week of the 14th" touched exactly those events, and once
that week is past there's nothing left to expire. The bound on the selector is
the bound on the amendment.

```
field_contributions(event_uuid, field, value, source_id, trust_tier,
                    observed_at, contribution_id, superseded_by)
```

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

---

## 7. What exists, and what the rest would need

Everything above is a design contract. As of the digest work, exactly one arrow
in it is built.

**Built.** calsync → room, one direction, no reply expected:

- `matrix.py` holds the connection settings (`matrix_homeserver`,
  `matrix_user_id`, `matrix_room_id`, `matrix_secret_ref`) and verifies them
  against the homeserver — separating the four things that go wrong: unreachable
  server, invalid token, token belonging to a different account, account not in
  the room. The token lives in the secret store, never the database.
- `matrix.send` posts a message. Its transaction id is derived from what the
  message is *about* rather than randomly, so a retry cannot double-post.
- `digest.py` renders what is on in the next day, re-deriving from the feeds
  because the calendar holds renders and `event_state` holds no content (§1 of
  [API.md](API.md) refuses this to Hermes for the same reason).

**Also built, and it changes the plan below.** Events calsync cannot *place* —
an unrecognised event type, a fixture where neither side is recognisably ours —
now wait in an `enrichment` collection rather than being filed under a guess,
and `/review` is where a human answers the question that releases them
(`Event.unresolved`, `routing.collection_for`). That is the human-in-the-loop
half of everything below, and it works with no agent at all.

It matters for the ordering because **the task-dispatch flow does not need the
inbound direction.** calsync posts a question; Hermes answers *on the API*; the
answer sits pending until a human approves it on `/review`. Nothing reads the
room. So of the three blockers below, only the first applies to that flow, and
the identity model is not needed for it either: authority comes from the API
token, and a bounded answer to a question calsync itself asked is not a proposal
needing an approval gate — it is a task resolution, and the human approval is
the gate.

**Not built, and why.** The rest of this document is the *inbound* direction —
Hermes proposing from pasted text, you approving, amendments landing — and it is
blocked on things code cannot decide:

1. **There is no API.** §2 and §3 both assume proposals go through one, with
   capability-scoped task tokens. Configuration was moved out of that API
   deliberately ([API.md](API.md)), but proposals were not: the review gate is
   the whole point, and it is structural rather than conventional.
2. **Approval needs an identity model.** "Nothing but a human approves" needs a
   way to tell a human's message from an agent's in a room they share. Matrix
   user ids are the obvious answer and they are also spoofable by anyone who
   gets the token, which is why §1 exists and why it is not a code task.
3. **Amendment blast radius (§149) is a policy, not a function.** How many
   events one pasted message may touch before it needs a second confirmation is
   a household's risk appetite.

**What would come next, in order, if it does get built:**

Two orderings, because they are different sizes. The **task-dispatch** flow —
calsync asks, Hermes answers, you approve — needs no inbound Matrix at all:

| Step | Needs | State |
|---|---|---|
| Hold what cannot be placed | An enrichment collection and a review queue | **built** |
| Notify that something is waiting | A Pushover trigger, once per question | **built** |
| Post the question to the room | Outbound only; `matrix.send` already exists | |
| Accept an answer | `POST /v1/tasks/{id}/result`, stored pending, never applied | |

The **proposal** flow — Hermes extracting events from a pasted PDF — is the
larger one and still needs everything above it:

| Step | Needs |
|---|---|
| Read messages from the room | A sync loop against `/sync`, and a decision about which messages are for calsync at all |
| Turn a message into a proposal | The proposals table and API from [API.md](API.md) |
| Show a proposal for approval | The identity model in §1 |
| Apply an approved proposal | The amendment path in §3, and the blast-radius policy |

Until step 1, the room is somewhere calsync talks and nobody replies — which is
a useful thing on its own, and honest about being only that.
