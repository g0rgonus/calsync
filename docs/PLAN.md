# calsync — Family Calendar Sync Platform

Status: planning. Nothing built yet.

## 1. The plan as stated

> ingestion (3rd-party kid platforms + AI PDF parsing) → middleware normalized calendar → Mac script syncs into iCloud → web UI to manage feeds

The four-layer shape is right. The gaps below are mostly in the seams between
layers, not in the layers themselves.

## 2. Revised architecture

```
  WRITERS                    POLICY                    STORES
  ───────────────────────────────────────────────────────────────────────────
  Hermes (PDF/photo) ─┐                            ┌─→ Radicale (CalDAV)
  Email worker       ─┤                            │   accepted events only
  ICS pollers        ─┼─→   calsync API      ──────┤   /games  /practices
  Scrapers           ─┤   (the ONLY writer)        │   (mirrors iCloud 1:1)
  Matrix bot         ─┤          │                 │
  Web UI (feeds)     ─┘          │                 │
                                 │                 │
                     dedup, validation,            └─→ SQLite
                     is_game routing,                  raw docs, extractions,
                     confidence gating,                sources, proposals,
                     provenance, tz rules              sync state, venues
                                 │
                                 ▼
                          review queue ──→ web UI
                                                          CONSUMERS
                                 Radicale                 iCloud: Games      ─┐
                                    │                     iCloud: Practices  ─┼→ family
                                    ├─→ iCloud push ────→ (already shared)    ┘
                                    ├─→ tokenized ICS ──→ outside the share
                                    └─→ your own devices, direct (staging/debug)
```

Radicale collections **mirror the iCloud calendars 1:1** (`/games`,
`/practices`) rather than splitting by child. Sync becomes a straight
collection→calendar map, and the reclassification-move logic
([NAMING.md §5](NAMING.md)) is identical on both sides instead of being a
regroup in the middle. Per-child views come from queries and ICS feeds, not
from the storage layout.

**Self-hosted CalDAV as the event store — yes.** This is better than a bare
relational store. iCalendar already carries more of the needed semantics than
I initially credited: `UID` for identity, `SEQUENCE` for versioning,
`STATUS:CANCELLED` for tombstones, `RECURRENCE-ID` for single-instance
overrides, and arbitrary `X-` properties for provenance
(`X-CALSYNC-SOURCE-ID`, `X-CALSYNC-CONFIDENCE`, `X-CALSYNC-DOC-ID`). File-backed
servers store one `.ics` per event in a directory you can put under git, which
gives you human-readable diffs and free version history.

The real limits are querying and non-event data — see §4.

**But the API is the only writer.** Nothing writes to CalDAV directly, not
Hermes, not the pollers, not you. Every write goes through the API so dedup,
validation, timezone rules, confidence gating, and provenance capture happen in
exactly one place. If an agent can write straight to the calendar, all of that
becomes advisory, and the review queue stops meaning anything.

**The middleware, API, and web UI are one service.** Radicale and the Matrix
bot run alongside as separate processes. Don't build five deployables.

**The Matrix room is the daily interface** ([MATRIX.md](MATRIX.md)): capture
(drop a PDF or photo), review (👍 to approve), amendments from pasted coach
messages, and alerting. The web UI owns configuration — adding a feed and
binding it to a kid and sport, venue pin placement, the adoption table, source
health. Chat for the loop you run daily, web for the things you set up once.

## 3. What's missing

### A. Ingestion

**A1. Email ingestion is the highest-value channel and it's not in the plan.**
Youth sports platforms without APIs almost all send email: schedule posts,
change notices, rain-out cancellations, tournament brackets. A dedicated
inbox (`cal@yourdomain`) that you set forwarding rules into covers every
platform at once, including ones you haven't thought of yet. Build this
before you build any scraper. Same AI extraction path as PDFs.

**A2. Photos/screenshots, not just PDFs.** Coaches post a picture of a
whiteboard or a paper schedule in a group chat. Same extraction pipeline,
vision input. Cheap to add if the pipeline takes bytes + mimetype rather than
"a PDF."

**A3a. Source *shape* matters as much as source tier.** Independent of how you
fetch it, a source is one of:

- **`feed`** — polled, re-asserts full state every run (ICS, API). Changes
  arrive through the feed itself.
- **`document`** — read once, never re-asserts (a season PDF). Changes arrive
  *out of band*: email, text, a coach's message pasted into chat.
- **`relay`** — human-forwarded messages. The change channel for document
  sources.

Swim is `document` + `relay`: the season comes from a PDF, every subsequent
change comes by email or text. That combination is probably the common case for
youth sports, not the exception — and it means the amendment path
([MATRIX.md §3](MATRIX.md)) isn't a convenience feature, it's the only way swim
stays correct after week one.

Shape drives conflict resolution (§B3), health monitoring (§D1), and whether
an activity needs a working relay path at all.

**A3. Source capability tiering.** Write the adapter interface so each source
declares which tier it supports, and always prefer the highest:

1. Official API (OAuth) — TeamSnap has one; verify others individually.
2. ICS / webcal export URL — SportsEngine, most school districts, many leagues.
3. Authenticated scrape (headless browser + stored session).
4. Email notifications.
5. Manual upload.

**Do the source survey before writing code.** Player360 is **confirmed** to
publish a token'd ICS feed — findings and adapter notes in
[sources/player360.md](sources/player360.md). For TeamReach, do not assume an
API exists — check for an ICS export in team settings first, then fall back to
email. Scrapers for these are tier 3 for a
reason: they break on every UI change, may violate ToS, and MFA will lock you
out eventually. Treat any scraper as a temporary bridge with a known expiry.

**A4. Store raw payloads immutably before parsing.** Every fetched page,
email, and PDF lands in blob storage with a hash, keyed to an ingest run. This
is what lets you re-run extraction with a better prompt across your entire
history without re-fetching — which you *will* want to do, repeatedly, in the
first month.

### B. The core (this is where the real work is)

**B1. Entity resolution, dedup, and adoption.** Full spec in
[MATCHING.md](MATCHING.md).

The same Saturday game arrives from three sources — a TeamReach post, the
August season PDF, an email reminder — and the shared calendars *already*
contain hand-created events for the same activities. Both are the same
question, so one matcher serves both: a blocking key on
`(local_date, is_game)`, then a weighted score over time proximity, opponent
similarity, child name, sport, and venue.

A single tuple can't do it, because the high-signal field differs by type:
games have opponents (`date, time±15m, opponent` is nearly conclusive),
practices don't (`date, child, activity, time±60m`, and hand-entered times
drift). It's a cascade, strict → loose, and the loosest tier only ever
produces review candidates.

Keep an explicit merge record so you can see *why* two things were joined, and
unjoin them in the UI.

**B2. Event versioning and supersession.** Rescheduled games are the normal
case, not the edge case. An event needs a version chain so "6pm game moved to
7:30" updates the existing calendar entry rather than adding a second one.
Related: **cancellation tombstones**. A rain-out must propagate a *delete* to
iCloud. Naive one-way sync only ever adds, which is the single most common way
these systems quietly fail.

**Never trust upstream change signals.** Player360 sets `SEQUENCE` equal to
`unix(LAST-MODIFIED)` and touches every event 2–3 seconds after it ends, so
both fields churn on events that did not change
([sources/player360.md](sources/player360.md)). Diff on our own content hash
over the fields we care about, and manage our own `SEQUENCE` rather than
propagating theirs — otherwise subscribers get change notifications for games
that already happened.

**Silent cancellation needs a mass-disappearance guard.** Feeds may signal
cancellation only by dropping the event, which makes a truncated or
wrong-scope `200` response indistinguishable from "the season was cancelled."
Before any delete path goes live: require a structurally valid `VCALENDAR`,
and if a poll shows >20% of an activity's known future events missing (or >3
in one poll), cancel nothing, hold prior state, and alarm.

**B3. Source trust ranking for conflict resolution.** When the PDF says 6pm and
the email says 7pm, something has to decide. Rank sources per activity
(human-relayed coach message > official API > email from the org > parsed PDF >
scrape), prefer recency within a tier, and surface unresolved conflicts to the
review queue rather than silently picking.

This mechanism also absorbs amendments, so they need no separate override
layer: a pasted coach message is a tier-1 field contribution and simply
outranks a stale poll. Critically, a **re-extraction carries the original
document's timestamp**, not today's — otherwise replaying extraction with an
improved prompt (§A4) would silently wipe every amendment.

The one case needing a human: a lower-tier source producing a value that is
both newer than the current winner and different from it — a revised PDF in
October. Don't pick silently; flag it.

**B4. A human review queue.** AI extraction will be wrong sometimes —
misparsed dates, wrong year, hallucinated venues, a bracket read as a
schedule. Anything below a confidence threshold, and anything that conflicts
with an existing event, must land in a "needs review" list in the web UI and
**not** flow to your calendar until approved. Approve / edit / reject, with
the source document rendered next to the extracted fields.

This is a must-have, not a v2 nicety. Without it, one bad parse teaches you to
distrust the whole system, and then you stop using it.

**B5. Timezone and recurrence correctness.** Store IANA timezone plus the
original raw string, always. Watch for: DST transitions inside a season
("every Tuesday 6pm" through November), all-day vs timed, multi-day
tournaments, doubleheaders, and away games in a different timezone. Generate
RRULEs carefully or expand to concrete instances — for youth sports, expansion
is usually safer, because half of a "recurring" practice series gets
individually moved anyway.

**B6. Venue normalization with real coordinates.** Full spec in
[NAMING.md §4](NAMING.md). Short version: `LOCATION` needs a real address plus
`GEO` and `X-APPLE-STRUCTURED-LOCATION` to render as a tappable map with
travel-time alerts. Coordinates matter more than the address — "Field 4" is a
corner of a large park, and the park's street address routes you to the wrong
entrance. LLMs disambiguate the string; a real geocoder produces the lat/lon; a
human confirms the pin once per venue. Never let a model emit coordinates.

**B7. Routing is one predicate: `is_game`.** Games go to `Games`; everything
else — practice, photos, banquet, tryouts, unknown — goes to `Practices`. That
makes misclassification cheap and self-correcting, so classification must never
block delivery. Presence of an opponent is a near-perfect signal, so rules
handle most of it and the LLM only sees genuine ambiguity.

One implementation trap: collections are separate CalDAV URLs, so
reclassifying an event between calendars is delete-then-create with a new UID,
not an update — and not a cancellation tombstone. See
[NAMING.md §5](NAMING.md).

### C. Delivery to iCloud

**C0. iCloud push is required early — it is not deferrable.** Earlier revisions
of this doc suggested adding Radicale directly as a phone CalDAV account and
putting off the iCloud work. That assumed no sharing was set up. It is:
`Practices` and `Games` already exist in iCloud and are already shared with
family. A Radicale-direct account only reaches devices on the tailnet, and it
can't reuse the sharing that already works. iCloud is the delivery target from
Phase 1.

Radicale-direct is still worth adding for yourself as a staging and debugging
surface — it shows you exactly what the store holds before it's pushed — but
it's a developer convenience, not the delivery path.

**Consequence: the calendar axis is event type, not child.** Kid identity has
to live in the event title. See [NAMING.md](NAMING.md).

**Consequence: the target calendars already contain human-created events.**
The "never touch what we didn't create" invariant (C3) is now the highest-risk
item in the project — a sync bug deletes your spouse's entries, in a calendar
several people rely on. Treat `sync_state` as the only ownership authority and
ship `--dry-run` before any write path goes live.

**C1. You probably don't need the Mac.** Three options, in order:

- **CalDAV directly to `caldav.icloud.com` with an app-specific password.**
  Runs server-side, no Mac awake, no TCC permission prompts, no breakage on
  macOS updates. Recommended default.
- **EventKit script on the Mac** (Swift or JXA). Full local access, but
  requires the Mac awake and logged in, needs automation permissions, and
  tends to break on OS upgrades. Good fallback, not a good primary.
- **Subscribed ICS calendar in iCloud.** Simplest to build and read-only by
  design — but iOS refreshes subscribed calendars lazily and throttles them
  regardless of the configured interval, and alerts on subscribed events are
  unreliable. Fine for grandparents, not for your own logistics.

**C2. Sync state must be persisted.** Keep a mapping table of
`canonical_event_id → (target, remote UID, ETag, last_synced_hash)`. Without
ETags you can't do safe updates or deletes, and you'll re-push unchanged
events forever.

**C3. Hard invariant: deletion requires a source.** Write only to `Games` and
`Practices`, never to your personal calendar, and refuse to modify any remote
event whose UID isn't in `sync_state`.

The stronger rule, because `Practices` also holds haircuts and dentist
appointments: **calsync manages sourced events only.** Hand-created events are
invisible to it — not read into a model, not classified, not enhanced, not
touched. Adding a haircut in Calendar.app in ten seconds stays exactly that.
An event is cancelled only when its upstream source stops reporting it; no
source, no delete, ever. See [MATCHING.md](MATCHING.md).

Add a `--dry-run` that prints the diff. A sync bug that deletes real calendar
entries is the failure mode that ends the project.

**C4. Decide the story for manual edits in iCloud.** If you drag a *sourced*
event in Calendar.app, the next sync reverts it. Simplest coherent answer:
sourced events are read-only mirrors, corrections happen in the web UI. This
only bites on events calsync owns — hand-created ones are never overwritten —
so the blast radius is small. Say it out loud so it's a decision, not a
surprise.

**C5. Backpressure and rate limits.** iCloud CalDAV throttles. Batch, back off,
and never let a full re-sync fire hundreds of requests in a burst.

### D. Operations

**D1. Failure notification, and it differs by source shape.** A silent failure
means a missed game — the exact thing this is built to prevent. But sources
come in three shapes, and only one of them can be monitored for freshness:

| `shape` | Example | Health signal |
|---|---|---|
| `feed` | ICS poller, API | **Staleness.** `last_successful_run` going quiet, HTTP errors, auth expiry. |
| `document` | Swim season PDF | **Coverage.** A PDF is read once; "no update in 3 weeks" is normal, not an alarm. |
| `relay` | Coach email, chat paste | **Coverage.** Arrives when it arrives. |

Applying staleness alarming to a document source produces nothing but false
alarms, and worse, trains you to ignore the channel. The useful signal there is
a **coverage gap**: *this activity is in season and has no events in the next
14 days.* That catches the real failure — the swim season rolled into a new
session and nobody sent you the new PDF — which no amount of polling would ever
surface.

Alarms go to the Matrix room ([MATRIX.md](MATRIX.md)). Heartbeat the worker
itself separately.

**D2. Secrets management.** You'll be holding portal logins, OAuth tokens, and
an iCloud app-specific password. Not `.env` in git — use SOPS+age, 1Password
CLI, or your host's secret store. Plan for the re-auth flow when MFA or a
password change locks a source out.

**D3. Auth on the web UI.** This is your kids' names, schedules, and physical
locations with timestamps. It cannot be a public URL. Pragmatic answer:
Tailscale-only, no public ingress. If you want it reachable off-tailnet, real
auth with 2FA.

**D4. Shadow mode before writing anything.** Run the full pipeline into a
scratch calendar for a week and diff against reality before pointing it at
your actual iCloud account.

**D5. Test corpus.** Save real PDFs, emails, and pages as fixtures with
expected extraction output. Golden-file tests are the only way to change an
extraction prompt without silently regressing three other formats.

### E. Product gaps

**E1. Distribution beyond you.** Spouse, grandparents, kids' own phones. Per
-person filtered ICS feed URLs (tokenized) is cheap and covers most of it.

**E2. Per-child, per-activity calendar mapping.** Decide whether each kid gets
a calendar, each activity does, or both — this determines the color-coding on
your phone, which is most of the perceived quality.

**E3. Default alarms per activity type.** Practices get a 30-minute warning;
away games get 90 minutes plus travel time. Set `VALARM` at sync time from an
activity-level policy.

**E4. School calendars and no-school days.** The actual driver of family
schedule chaos. Usually a plain ICS URL from the district — cheapest win
available and worth doing in Phase 1.

**E5. An "unresolved" bucket.** Email arrives about a kid or team you can't map
to a known entity. It needs somewhere to go, and a UI to bind it to the right
child/team, which then teaches the mapping for next time.

### F. Agent writers (Hermes)

Full contract in [API.md](API.md). The design constraints that matter:

**F1. Agents submit proposals, not events.** `POST /v1/proposals` — the API
decides whether it auto-accepts, queues for review, or rejects. Hermes should
not be deciding what lands on your phone, and this means it doesn't need to
know anything about dedup, trust ranking, or your existing calendar.

**F2. Idempotency is mandatory, because agents retry.** Every proposal carries
a client-supplied key derived from `(document_sha256, event_ordinal)`. Re-running
Hermes over the same PDF must be a no-op, not a second set of events. This is
the single most likely way to end up with a duplicate-riddled calendar.

**F3. Two-step: upload the document, then propose against it.** `POST
/v1/documents` returns a `document_id`; proposals reference it. This gets you
the immutable raw copy (§A4), lets the review UI render the source PDF beside
the extracted fields, and dedups re-uploads by hash for free.

**F4. Validate hard, reject with structured reasons.** Agents emit naive
datetimes, ambiguous years, `6:00` with no meridiem, and `TBD` times. Require
explicit IANA timezone and reject rather than guessing — an agent can correct a
structured 422 far better than you can debug a silently-wrong event six weeks
later. Ambiguity that can't be resolved should become a low-confidence proposal
with a flag, not a guess.

**F5. Per-field confidence, plus `raw_text` on every proposal.** Date
confidence and venue confidence are rarely the same number, and the review UI
should highlight only the weak fields. `raw_text` (the snippet the extraction
came from) is what makes a bad parse debuggable.

**F6. Scoped token, proposal-only.** Hermes gets a token that can create
documents and proposals but cannot approve them or write events directly. Cheap
privilege separation that makes the review gate structural rather than
conventional.

## 4. What CalDAV holds, and what it can't

**Radicale holds:** accepted events, one collection per child × activity.
Canonical `VEVENT`s with `X-` provenance properties. This is the thing your
phone talks to.

**SQLite holds everything that isn't an event.** CalDAV's query model is a
time-range `REPORT` with property filters — no joins, no aggregates, no
"proposals below 0.7 confidence that conflict with an accepted event." At
family scale you *could* dump the whole collection and filter in memory, but
these tables have to exist regardless because none of it is a calendar event:

```
children          (id, name, nicknames, birth_order, color)
activities        (id, child_id, name, official_name, sport, emoji,
                   short_name, season_start, season_end, tz, alarm_policy)
activity_aliases  (activity_id, alias, source)   -- "U10DA" in a coach email
                                                 -- must resolve to "Rush"
sources           (id, activity_id, kind, shape, tier, config_json, secret_ref,
                   trust_rank, last_success_at, health)
                   -- shape: feed | document | relay
ingest_runs       (id, source_id, started_at, status, error)
raw_documents     (id, sha256, mimetype, blob_uri, received_at, source_id)
extractions       (id, raw_document_id, extractor, prompt_version,
                   payload_json, created_at)
proposals         (id, extraction_id, idempotency_key, state, confidence,
                   payload_json, conflict_with_uid, decided_at, decided_by)
event_index       (uid, collection, is_game, dedup_key, starts_at, venue_id,
                   sequence, status, last_hash)   -- query cache over CalDAV
venues            (id, canonical_name, short_name, address, lat, lon,
                   pin_confirmed_by_human, geocoder, geocode_confidence)
venue_aliases     (venue_id, alias, source)
sync_state        (uid, target, calendar, origin, remote_uid, etag,
                   last_synced_hash)   -- origin: ingested | adopted
adoptions         (uid, calendar, icloud_uid, matched_proposal_id, score,
                   tier, adopted_at, adopted_by, original_ics)
tasks             (id, type, event_uuid, context_json, state, token_hash,
                   expires_at, resolved_by, result_json)
amendments        (id, source_document_id, selector_json, patch_json,
                   rationale, applied_at, applied_by, prior_ics, undone_at)
field_contributions(event_uuid, field, value, source_id, trust_tier,
                   observed_at, contribution_id, superseded_by)
```

Three notes:

- **Proposals are not events.** They live in SQLite until approved, then get
  written to CalDAV. A proposal may be missing a time or a venue — an invalid
  `VEVENT` — so it has no business in the calendar store. Clean line, and it
  keeps unreviewed AI output physically incapable of reaching your phone.
- **`event_index` is a derived cache**, rebuildable by walking the collections.
  It exists so dedup lookups and conflict checks are a single indexed query
  instead of a full CalDAV dump. Never the source of truth.
- **`raw_documents` + `extractions` separate from events** is what makes
  re-parsing history possible when you improve a prompt. Don't collapse them.

## 5. Suggested stack

- **CalDAV server:** **Radicale** — Python, file-backed, trivial to run, and
  its hook config can `git commit` on every change for free history. Xandikos
  is natively git-backed if you'd rather. Baikal is more spec-complete but
  heavier; only worth it if a client misbehaves against Radicale.
- **Runtime:** Python pairs naturally here (same as Radicale, best PDF/vision
  tooling). TypeScript is fine if you'd rather share a language with the UI.
- **DB:** SQLite. Single box, single writer, family scale. Postgres buys you
  nothing yet.
- **Queue/schedule:** cron + a jobs table. Don't reach for Temporal.
- **Extraction:** Claude with structured outputs; version every prompt and
  record `prompt_version` on each extraction.
- **Libs:** `icalendar` / `ical.js`, `caldav` client, `vobject`.
- **Hosting:** home box behind Tailscale. Low-throughput; no cloud needed.

## 6. Phased roadmap

**Phase 0 — Source survey (do this first, before code).**
For each real source: does it have an API? An ICS export buried in team
settings? Does it email? Write the findings down. This determines how much of
the rest is even needed — if three of your five sources turn out to have ICS
exports, the AI ingestion layer becomes a fallback rather than the centerpiece.

**Phase 1 — Radicale + API + one source + iCloud push.** Stand up Radicale, the
API (`/documents`, `/proposals`, `/events`), one ICS poller, the title renderer,
and the iCloud CalDAV writer with `sync_state` and `--dry-run`. Run dry against
the real shared calendars for a week before enabling writes. No UI, no AI yet.

**Phase 1.1 — Adoption pass.** Snapshot both calendars to git, then reconcile
the existing hand-created events ([MATCHING.md §3](MATCHING.md)). Must happen
before the first real write, or calsync creates its copy next to yours. Needs
just enough UI to approve matches — a printed table plus an approve endpoint is
enough; the full review UI comes in Phase 2.

This phase is bigger than the previous draft because the iCloud writer and
adoption both moved into it, but it's the right scope: the write path is the
risky part and everything else stacks on top of it.

**Phase 1.5 — Venue pipeline.** Alias table, LLM disambiguation, geocoder,
pin confirmation UI. Small, and it's most of the perceived day-to-day value.

**Phase 2 — Hermes + Matrix bot + review.** `calsync_bot` in the room, task
dispatch to Hermes, document/photo capture from chat, reaction-based approval.
Raw document storage, confidence scoring, idempotency, `pending_review`. The
room is the review surface; no web UI needed yet.

**Phase 2.5 — Amendments + overrides.** Pasted coach messages patching live
events, with the override layer so source polls don't revert them
([MATRIX.md §4](MATRIX.md)). Do not ship amendments without overrides — they
flap.

**Phase 2.75 — Email ingestion.** Same proposal path, different producer.

**Phase 3 — Web UI.** Feed management (add a feed, bind it to a kid and sport),
venue pin placement, adoption table, source health dashboard. Configuration,
not daily driving.

**Phase 4 — Dedup and merge.** Multi-source overlap, trust ranking, conflict
surfacing. Deliberately after Phase 3, because you need the UI to inspect
merges.

**Phase 5 — Distribution.** Tokenized ICS feeds for family outside the iCloud
share, alarm policies, school calendars, digests.

Scrapers get built only when a specific source justifies one, and each is
written expecting to be thrown away.

## 6a. Scope reality check

Written down because a half-built system in November, when the season ends, is
the realistic failure mode — not a system that doesn't work.

### Where the value actually is

| Component | Value | Effort |
|---|---|---|
| PDF/photo → events | high | medium |
| Geocoded, tappable locations | **highest per unit effort** | low |
| Normalized titles | high | low |
| iCloud write path w/ sync_state | required for any of it | medium |
| Amendment flow (chat → API) | medium | high |
| Adoption matcher | low | medium |
| Trust ranking / field contributions | low unless multi-source | medium |
| Radicale as store | convenience | low |

Roughly **80% of the value is in the first four rows**, which is about a
quarter of the design.

### What to cut first if you're running out of steam

1. **The adoption matcher.** For a one-time setup problem, deleting the
   hand-created sports events for the upcoming season and letting calsync
   repopulate takes ten minutes. Building a fuzzy matcher takes a weekend. The
   matcher only earns its place for *ongoing* cross-source dedup — so if
   Phase 0 finds one source per activity, skip it entirely.
2. **Trust ranking and field contributions.** Dead code unless an activity has
   two sources that disagree. Ship "last write wins, flagged" and add ranking
   when you actually observe a conflict.
3. **Radicale.** A convenience — git history, standard tooling, direct client
   access — not a necessity. SQLite → iCloud directly is fewer moving parts.
4. **The amendment flow.** Editing four swim practices by hand in Calendar.app
   takes about a minute. The chat path has to be *more reliable than that* to
   be worth using, which is a high bar. It earns its place when changes are
   frequent and multi-event; it does not earn its place as a demo.

### The assumption most likely to be wrong

This architecture is sized for **reconciling several continuously-updating
feeds**. Swim already isn't one. If Phase 0 finds that TeamReach and Player360
also have no ICS export or API, then *every* source is `document` + `relay`,
and there is nothing to reconcile — no polling, no flapping, no trust conflicts,
no dedup across sources.

In that world the honest system is much smaller: **a good PDF ingester, a title
and venue normalizer, an iCloud writer, and a chat path for changes.** Half of
this document becomes unnecessary. Do Phase 0 before committing to any of it.

### What it doesn't do

It won't pay for itself in time saved. A season is maybe 160 events across the
family; entering them by hand is a few hours a year against a build measured in
weeks. The return is in the changes nobody re-enters by hand — the rained-out
game, the moved pool — and in not having a kid on a curb. Budget it as a
reliability project you enjoy building, not a labor saver, and the scope
decisions get easier.

## 7. Open questions

1. Which platforms are actually in play, and how many kids/activities? (Drives
   Phase 0.)
2. Is the Mac a hard requirement, or was it just the path you knew? CalDAV
   server-side is more reliable if you're open to it.
3. Kids' first names and a fixed ordering, plus which sports each plays — needed
   to pin down the title convention and emoji map.
4. Do the existing `Practices` / `Games` calendars already contain hand-entered
   events for these same activities? If so, Phase 1 needs a one-time
   reconciliation pass, not just a clean write path.
5. Are you the owner of both shared calendars, or is one shared *to* you? Write
   access via CalDAV depends on it.
6. Is "our events are read-only mirrors, corrections happen in the web UI"
   acceptable? Family members editing a shared event in Apple Calendar will get
   reverted on next sync.
