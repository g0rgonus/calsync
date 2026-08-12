# Event matching: dedup and adoption

Two problems, one matcher:

1. **Cross-source dedup** — the same game arrives from a PDF, an email, and a
   poller ([PLAN.md §B1](PLAN.md)).
2. **Adoption** — the shared iCloud calendars already contain hand-created
   events for these same activities. Ingesting on top of them produces a
   duplicate sitting next to yours.

Both are "does this candidate refer to the same real-world event as that
existing one." Same scoring machinery, different callers and thresholds.

## Ownership modes

The `Practices` calendar is really a *kid logistics* calendar. Alongside
practices it holds haircuts, dentist, orthodontist, birthday parties, school
concerts — hand-created, with no upstream source, and people will keep adding
them by hand forever.

That breaks a model where "in `sync_state`" means "calsync owns it." Split
ownership into three modes:

| Mode | calsync may | calsync may never |
|---|---|---|
| `sourced` | create, update, cancel | — |
| `enhance` | add `GEO` / structured location / alarms; prefix a missing child name | rewrite the body text, delete, cancel |
| `untouched` | read | write anything |

**The delete rule, stated once and absolutely:**

> calsync may cancel or delete an event only when `mode = sourced` **and** its
> upstream source stopped reporting it. No source, no delete — ever. Not for
> looking stale, not for failing reconciliation, not for looking orphaned.

Deletion authority comes from *having a source*, not from being known. A
haircut has no source, so nothing can ever justify removing it. Stamp
`X-CALSYNC-MODE` on the event and keep `mode` in `sync_state`, with the
`sync_state` value authoritative (clients drop unknown `X-` properties).

## Where it runs

Not in the sync client. The CalDAV/EventKit layer's one job is to be dumb and
safe: *never touch a UID that isn't in `sync_state`*. The moment it starts
fuzzy-matching, it becomes a second decision-maker and that invariant erodes —
which is the failure mode that deletes your spouse's entries.

Split it:

- **Sync layer reads.** Pull existing VEVENTs out of the two calendars. EventKit
  or CalDAV both work; CalDAV keeps it server-side and Mac-free.
- **Core decides.** The matcher scores candidates and writes `sync_state`.
- **Sync layer writes**, and only ever to UIDs `sync_state` already knows.

---

## 1. Why one tuple doesn't work

The high-signal field differs by event type:

- **Games have an opponent.** `(date, time±15m, opponent)` is nearly
  conclusive — you don't play two different teams at 2pm on the same Saturday.
- **Practices don't.** There's no distinguishing field beyond
  `(date, child, activity, time)`, and hand-entered practice times drift.

So it's a **cascade of tuples**, strict → loose, each with its own confidence.
First tier that fires wins.

| Tier | Tuple | Applies to | Result |
|---|---|---|---|
| 0 | `X-CALSYNC-UID` present | anything | Ours already. Link, no scoring. |
| 1 | `(date, start±15m, is_game, opponent~)` | games | Auto-match |
| 2 | `(date, start±60m, child, activity)` | practices | Auto-match if score clears |
| 3 | `(date, child)` | anything | **Candidate only** — never auto |

Tier 3 never auto-matches. It exists to surface "there's something else for
this kid that day" to a human.

---

## 2. Blocking key, then score

Comparing every candidate against every existing event is O(n²) and
unnecessary. Block first:

**Blocking key (must hold, or not a candidate):** same local date *and* same
target calendar (`is_game`). Everything else is scored.

Widen the date block by ±1 day for events after 21:00 or before 06:00 — a late
game near midnight can land on either side depending on whose timezone parsed
it.

### Scored signals

| Signal | Weight | Scoring |
|---|---|---|
| Time proximity | 0.35 | `1.0` ≤15m, `0.6` ≤60m, `0.3` ≤180m, `0` beyond. All-day → flat `0.5` (carries no time info). |
| Opponent | 0.25 | Token-set similarity ≥0.8 after normalization. Games only; redistribute weight if absent. |
| Child | 0.20 | Any known first name or nickname found in `SUMMARY`. |
| Sport/activity | 0.10 | Sport word or emoji present in `SUMMARY`. |
| Venue | 0.10 | Alias or token overlap. Weak signal, good confirmer. |

**Thresholds:** ≥0.85 auto-match · 0.50–0.85 review queue · <0.50 ignore.

### Normalization before comparing

- **Opponent:** lowercase, strip club suffixes (`FC`, `SC`, `United`, `Utd`,
  `Club`, `Academy`), strip punctuation, compare on token sets. `Northside
  United` / `N. Side Utd` / `northside` all collapse together.
- **Child:** maintain a nickname list per child. Hand-entered titles use
  whatever was fast to type.
- **Date:** in the event's local timezone, never UTC.
- **Time:** hand-created events frequently encode **arrival time, not start
  time** — "be there 45 min early" becomes a 1:15 entry for a 2:00 game. That's
  why the ±60m band gets meaningful credit instead of being treated as a miss.

---

## 3. The adoption pass

A **one-time, bounded** operation per calendar at setup. Not an ongoing mode.

```
1. Snapshot.     Export both calendars to .ics, commit to git. This is your undo.
2. Read.         Pull all events in [today, end_of_season].
3. Partition.    Ours (X-CALSYNC-UID) / candidates / clearly unrelated.
4. Score.        Run the matcher against ingested events.
5. Report.       Print a match table. Nothing is written yet.
6. Confirm.      You approve, edit, or reject each match in the UI.
7. Adopt.        Write sync_state rows, stash the original VEVENT verbatim.
```

### Rules

**Adoption can only link or leave alone. It can never delete.** An unmatched
hand-created event might be a game from a source you don't ingest, or a
dentist appointment. Leave it.

**Adoption means taking ownership.** After adopting, calsync will update and
potentially cancel that event. Store `original_ics` verbatim so any adoption is
reversible — you can restore the exact VEVENT you took over.

**Bias hard toward human confirmation.** This runs once over maybe 50–150
events. The automation never amortizes, so don't over-engineer it: a printed
table and a few minutes of clicking beats a clever classifier you'll debug for
an afternoon and use exactly one time.

```
adoptions(uid, calendar, icloud_uid, mode, category, matched_proposal_id,
          score, tier, adopted_at, adopted_by, original_ics)
```

### Renaming on adopt

An adopted event still has its hand-typed title (`Soccer game`, `NORA SOCCER!!`).
Decide per match whether to re-render it into the naming convention. Default to
yes — normalizing is the point — but make it a per-row toggle, since a few
hand-written titles carry detail the extraction didn't capture.

---

## 3a. Non-sport events: haircuts, dentist, birthday parties

### Keep them out of adoption entirely

An appointment must never match a sports proposal. The scoring mostly handles
this on its own — "Nora haircut" 5:00pm vs. an ingested 5:30pm soccer practice
scores ~0.41 (time 0.21 + child 0.20, nothing from sport, venue, or opponent)
and falls below the 0.50 floor. But "mostly" isn't a safety property, so add
an explicit gate:

**Categorize every unowned event first; only `sport` events are adoption
candidates.** A keyword list gets you most of the way — `haircut`, `dentist`,
`ortho`, `doctor`, `checkup`, `appt`, `birthday`, `party`, `sleepover`,
`recital`, `conference` — with an LLM fallback for the ambiguous remainder.
Anything not classified `sport` is excluded from matching, full stop.

**Run that classification locally.** These titles include medical appointments
for minors. Category detection belongs on keywords or local Hermes, not shipped
to an external API. Same rule as venue resolution: the *only* strings that
should reach a hosted model are venue names for geocoding, and even then, not
if the title implies a clinic.

### Don't retrofit — redirect

The instinct to run manual events through the tool is right, but retroactive
rewriting is the wrong shape:

- Rewriting text your spouse typed on a shared calendar is surprising, and if
  they edit it back you get a fight loop.
- Editing shared events bumps `SEQUENCE` and can fire change notifications to
  every subscriber. A bulk normalization pass over a season of history would
  spam the whole family at once.
- The marginal value is low. "Nora haircut" already reads fine — the naming
  convention exists to disambiguate *which kid* among a wall of sports events,
  and hand-typed entries usually already name the kid.

Better: make the web UI a first-class way to *create* these. An appointment
added through calsync is `sourced` from birth — normalized title, geocoded
clickable location, right alarms, no retrofitting. Existing entries stay as
they are. Going forward the good path is also the easy path.

### For events created outside calsync, enhance conservatively

Your spouse will keep using Apple Calendar, so `enhance` mode has to exist.
Split it, because the two halves have very different risk:

| Enhancement | Default | Why |
|---|---|---|
| Add `GEO` + `X-APPLE-STRUCTURED-LOCATION` from an existing `LOCATION` string | **on** | Additive, invisible, high value — you drive to the pediatrician once a year and don't know the way. |
| Add a `VALARM` if none exists | **on** | Additive, invisible. |
| Prefix a missing child name (`Dentist 3pm` → `Jack 🦷 Dentist 3pm`) | **review** | Genuinely useful — an unattributed appointment on a shared calendar is the actual failure case — but it's a text change, so confirm it. Often unattributable, in which case leave it. |
| Rewrite the title into the naming convention | **off** | Low value, high surprise. |

Enhancement is still a write to a shared calendar, so it goes through the same
machinery as everything else: `sync_state` row, `original_ics` snapshot,
`--dry-run` first. And throttle the initial pass — a few events per minute, not
a season in one burst — so subscribers don't get a notification storm.

## 4. After adoption: collision detection, not adoption

Once setup is done, everything in the calendar without a `sync_state` row is
assumed human and left alone permanently. But people keep hand-adding events
after the system is live, so the matcher stays wired in with a different
outcome:

> Before creating any new event, run the matcher against **unowned** events in
> the target calendar. Score ≥0.50 → don't create. Send it to review as a
> collision with a "this may already be on the calendar" flag.

Detection, never silent merge. The cost of a false positive is one review click;
the cost of a false negative is a duplicate in a calendar four people read.
