# calsync — Family Calendar Sync Platform

Status: planning. Nothing built yet.

## 1. The plan as stated

> ingestion (3rd-party kid platforms + AI PDF parsing) → middleware normalized calendar → Mac script syncs into iCloud → web UI to manage feeds

The four-layer shape is right. The gaps below are mostly in the seams between
layers, not in the layers themselves.

## 2. Revised architecture

```
 SOURCES                 INGEST              CORE                    DELIVERY
 ─────────────────────────────────────────────────────────────────────────────
 Official API      ─┐
 ICS/webcal URL    ─┤
 Authed scrape     ─┼─→  adapters  ─→  raw_documents  ─→  extraction  ─┐
 Email (forward)   ─┤    (per src)     (immutable)       (AI + rules)  │
 PDF / photo       ─┤                                                  ▼
 Manual entry      ─┘                                          canonical events
                                                                (versioned)
                                                                       │
                                     ┌─────────────────────────────────┤
                                     ▼                                 ▼
                              review queue                      sync targets
                            (low-confidence)                  ├─ iCloud (CalDAV)
                                     │                        ├─ ICS feed URLs
                                     ▼                        └─ Google (optional)
                              web UI (feeds, review, health)
```

Two structural changes from the original plan:

**The middleware is a database, not a calendar product.** If the normalized
layer is a Google/CalDAV calendar, you lose the fields that make the whole
thing work: source provenance, extraction confidence, event version history,
dedup keys, review state. Calendars can't hold that. Make Postgres the source
of truth and treat *every* calendar (including a Google mirror, if you want
one for family visibility) as an output.

**The middleware and the web UI are one service.** Don't build three
deployables. One app, one DB, a worker for scheduled ingest.

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

## 4. Data model sketch

```
children          (id, name, color)
activities        (id, child_id, name, season_start, season_end, tz, alarm_policy)
sources           (id, activity_id, kind, tier, config_json, secret_ref,
                   trust_rank, last_success_at, health)
ingest_runs       (id, source_id, started_at, status, error)
raw_documents     (id, ingest_run_id, sha256, mimetype, blob_uri, received_at)
extractions       (id, raw_document_id, model, prompt_version, confidence,
                   payload_json, created_at)
events            (id, activity_id, version, supersedes_id, status,
                   starts_at, ends_at, tz, venue_id, title, notes,
                   dedup_key, confidence, review_state)
event_provenance  (event_id, extraction_id, contribution)
venues            (id, canonical_name, address, lat, lon, aliases[])
sync_targets      (id, kind, config_json)
sync_state        (event_id, target_id, remote_uid, etag, last_synced_hash)
```

`raw_documents` + `extractions` being separate from `events` is what makes
re-parsing history possible. Don't collapse them.

## 5. Suggested stack

- **Runtime:** Python (best PDF/vision tooling) or TypeScript (one language
  across UI + worker). Either is fine; pick the one you'll actually maintain.
- **DB:** Postgres. `pgvector` later if you want fuzzy venue/team matching.
- **Queue/schedule:** start with cron + a jobs table. Don't reach for Temporal
  on day one.
- **Extraction:** Claude with structured outputs; version every prompt and
  record `prompt_version` on each extraction.
- **Calendar libs:** `ical.js` / `icalendar`, and a CalDAV client for iCloud.
- **Hosting:** a small VPS or a home box behind Tailscale. This is a
  low-throughput workload; it does not need cloud infrastructure.

## 6. Phased roadmap

**Phase 0 — Source survey (do this first, before code).**
For each real source: does it have an API? An ICS export buried in team
settings? Does it email? Write the findings down. This determines how much of
the rest is even needed — if three of your five sources turn out to have ICS
exports, the AI ingestion layer becomes a fallback rather than the centerpiece.

**Phase 1 — Walking skeleton.** One source (whichever has an ICS URL), DB,
canonical events, CalDAV push to a *scratch* iCloud calendar. No UI, no AI.
Proves the hardest part — reliable, idempotent, delete-correct calendar
writes — before anything else is built on top of it.

**Phase 2 — Email + PDF ingestion.** Inbox, raw document storage, AI
extraction, confidence scoring, review queue. First real UI: review + approve.

**Phase 3 — Web UI proper.** Feed management, source health dashboard, venue
and alias editing, child/activity mapping.

**Phase 4 — Dedup and merge.** Multi-source overlap, trust ranking, conflict
surfacing. Deliberately after Phase 3, because you need the UI to inspect
merges.

**Phase 5 — Distribution.** Tokenized ICS feeds for family, alarm policies,
school calendars, digests.

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
