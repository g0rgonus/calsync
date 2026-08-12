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
  Hermes (PDF/photo) ─┐
  Email worker       ─┤                            ┌─→ Radicale (CalDAV)
  ICS pollers        ─┼─→   calsync API      ──────┤   accepted events only
  Scrapers           ─┤   (the ONLY writer)        │   one collection per
  Web UI / manual    ─┘          │                 │   child × activity
                                 │                 │
                     dedup, validation,            └─→ SQLite
                     confidence gating,                raw docs, extractions,
                     provenance, tz rules             sources, proposals,
                                 │                     sync state, venues
                                 ▼
                          review queue ──→ web UI
                                                          CONSUMERS
                                 Radicale ──────────────→ iPhone/Mac CalDAV acct
                                    │                     (direct, via Tailscale)
                                    └─→ iCloud push ────→ family, off-tailnet
                                    └─→ tokenized ICS ──→ grandparents
```

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

**The middleware, API, and web UI are one service.** Radicale runs alongside as
a separate process. Don't build three deployables.

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

**A3. Source capability tiering.** Write the adapter interface so each source
declares which tier it supports, and always prefer the highest:

1. Official API (OAuth) — TeamSnap has one; verify others individually.
2. ICS / webcal export URL — SportsEngine, most school districts, many leagues.
3. Authenticated scrape (headless browser + stored session).
4. Email notifications.
5. Manual upload.

**Do the source survey before writing code.** For TeamReach and Player360
specifically, do not assume an API exists — check for an ICS export in team
settings first, then fall back to email. Scrapers for these are tier 3 for a
reason: they break on every UI change, may violate ToS, and MFA will lock you
out eventually. Treat any scraper as a temporary bridge with a known expiry.

**A4. Store raw payloads immutably before parsing.** Every fetched page,
email, and PDF lands in blob storage with a hash, keyed to an ingest run. This
is what lets you re-run extraction with a better prompt across your entire
history without re-fetching — which you *will* want to do, repeatedly, in the
first month.

### B. The core (this is where the real work is)

**B1. Entity resolution and dedup.** The same Saturday game will arrive from
three sources: a TeamReach post, the season PDF you uploaded in August, and an
email reminder. Without a dedup strategy you get triplicates in iCloud.

Needs a fuzzy match key — roughly `(child, activity, date, start_time ±90min,
venue_similarity)` — with a confidence score, plus an explicit merge record so
you can see *why* two things were joined and unjoin them in the UI.

**B2. Event versioning and supersession.** Rescheduled games are the normal
case, not the edge case. An event needs a version chain so "6pm game moved to
7:30" updates the existing calendar entry rather than adding a second one.
Related: **cancellation tombstones**. A rain-out must propagate a *delete* to
iCloud. Naive one-way sync only ever adds, which is the single most common way
these systems quietly fail.

**B3. Source trust ranking for conflict resolution.** When the PDF says 6pm and
the email says 7pm, something has to decide. Rank sources per activity
(official API > email from the org > parsed PDF > scrape), prefer recency
within a tier, and surface unresolved conflicts to the review queue rather
than silently picking.

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

**B6. Venue normalization with real addresses.** Put a full geocoded address in
the calendar `LOCATION` field, not "Field 4." That's what makes Apple Maps
travel-time alerts work, which is most of the day-to-day value. Keep a venue
table with aliases — "Riverside #4", "Riverside Park Field 4", and "RP4" are
one place.

### C. Delivery to iCloud

**C0. With Radicale as the store, you may not need iCloud at all — at first.**
iOS and macOS can add a CalDAV account directly (Settings → Calendar →
Accounts → Add Account → Other → Add CalDAV Account) pointed at Radicale over
Tailscale. That's real two-way sync, native alerts, native colors, zero sync
code. **Do this in Phase 1 and defer the iCloud push**, which is the most
dangerous code in the project.

You'll still want the iCloud path eventually, for reasons worth knowing up
front: family members who won't install Tailscale, reliability when the home
box is down (CalDAV clients cache, but stale), and iCloud family sharing. Build
it when you hit one of those, not before.

**C1. When you do add iCloud, you probably don't need the Mac.** Three options,
in order:

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

**C3. Hard invariant: never touch an event we didn't create.** Write only to
dedicated calendars (`Kids — Soccer`, `Kids — School`), never to your personal
one, and refuse to modify any remote event whose UID isn't in the sync table.
Add a `--dry-run` that prints the diff. A sync bug that deletes real calendar
entries is the failure mode that ends the project.

**C4. Decide the story for manual edits in iCloud.** If you drag an event in
Calendar.app, the next sync will revert it. Simplest coherent answer: our
calendars are read-only mirrors, you make corrections in the web UI. Say it
out loud in the design so it's a decision, not a surprise.

**C5. Backpressure and rate limits.** iCloud CalDAV throttles. Batch, back off,
and never let a full re-sync fire hundreds of requests in a burst.

### D. Operations

**D1. Failure notification.** A silent sync failure means a missed game — the
exact thing this is built to prevent. Track `last_successful_run` per source,
alarm on staleness (a source that normally updates weekly going quiet for two
weeks), and push a notification when a scraper breaks or credentials need
re-auth. Heartbeat the worker itself.

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
children          (id, name, color)
activities        (id, child_id, name, collection_path, season_start,
                   season_end, tz, alarm_policy)
sources           (id, activity_id, kind, tier, config_json, secret_ref,
                   trust_rank, last_success_at, health)
ingest_runs       (id, source_id, started_at, status, error)
raw_documents     (id, sha256, mimetype, blob_uri, received_at, source_id)
extractions       (id, raw_document_id, extractor, prompt_version,
                   payload_json, created_at)
proposals         (id, extraction_id, idempotency_key, state, confidence,
                   payload_json, conflict_with_uid, decided_at, decided_by)
event_index       (uid, collection_path, dedup_key, starts_at, venue_id,
                   sequence, status, last_hash)   -- query cache over CalDAV
venues            (id, canonical_name, address, lat, lon, aliases)
sync_state        (uid, target, remote_uid, etag, last_synced_hash)
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

**Phase 1 — Radicale + API + one source.** Stand up Radicale, the API with
`/documents`, `/proposals`, `/events`, and one ICS poller. Add the Radicale
account directly to your phone. No iCloud push, no UI, no AI. You get real
events on your device at the end of this phase, and the dangerous iCloud code
is deferred.

**Phase 2 — Hermes + review queue.** Point Hermes at the proposals endpoint.
Raw document storage, confidence scoring, idempotency, `pending_review`. First
real UI: review + approve, with the source PDF rendered alongside.

**Phase 2.5 — Email ingestion.** Same proposal path, different producer.

**Phase 3 — Web UI proper.** Feed management, source health dashboard, venue
and alias editing, child/activity mapping.

**Phase 4 — Dedup and merge.** Multi-source overlap, trust ranking, conflict
surfacing. Deliberately after Phase 3, because you need the UI to inspect
merges.

**Phase 5 — Distribution.** iCloud push (when you hit a Radicale-direct limit
from §C0), tokenized ICS feeds for family, alarm policies, school calendars,
digests.

Scrapers get built only when a specific source justifies one, and each is
written expecting to be thrown away.

## 7. Open questions

1. Which platforms are actually in play, and how many kids/activities? (Drives
   Phase 0.)
2. Is the Mac a hard requirement, or was it just the path you knew? CalDAV
   server-side is more reliable if you're open to it.
3. Google Calendar mirror for family visibility — wanted, or is iCloud + ICS
   feeds enough?
4. Who else needs read access, and on what devices?
5. Is "our calendars are read-only mirrors, edits happen in the web UI"
   acceptable, or do you need write-back from iCloud?
