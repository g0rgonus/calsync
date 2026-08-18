# Event matching: dedup and adoption

Two problems, one matcher:

1. **Cross-source dedup** — the same game arrives from a PDF, an email, and a
   poller ([PLAN.md §B1](PLAN.md)).
2. **Adoption** — the shared iCloud calendars already contain hand-created
   events for these same activities. Ingesting on top of them produces a
   duplicate sitting next to yours.

Both are "does this candidate refer to the same real-world event as that
existing one." Same scoring machinery, different callers and thresholds.

## Scope: calsync manages sourced events only

The `Practices` calendar is really a *kid logistics* calendar. Alongside
practices it holds haircuts, dentist, orthodontist, birthday parties, school
concerts — added by hand, in Calendar.app, in ten seconds, by whoever is
holding a phone.

**That workflow does not change.** Nobody opens a web app at the house to add
a haircut. calsync manages events that came from a platform, a PDF, or an
email; everything else is invisible to it.

| | calsync may | |
|---|---|---|
| **Managed** — in `sync_state`, has an upstream source | create, update, cancel | |
| **Everything else** — the default | **nothing** | not read into a model, not classified, not enhanced, not touched |

Two invariants, and they're the whole safety story:

> 1. calsync writes only to UIDs present in `sync_state`.
> 2. A UID enters `sync_state` only by being created from a source, or by
>    surviving the one-time adoption pass (§3).

Deletion authority follows from having a source: an event is cancelled only
when its upstream stops reporting it. A haircut has no upstream, so nothing can
ever justify removing it.

### What this simplification buys

Scoping to sourced events deletes a lot of machinery that was only there to
make touching manual events safe:

- No enhancement write path, and no risk of a bulk pass firing change
  notifications at every subscriber on the shared calendar.
- No ongoing categorization of every event on the calendar — so medical
  appointment titles for minors never go near a model, local or hosted, because
  nothing ever needs to look at them.
- No `enhance` mode, no title-rewrite toggles, no per-event opt-outs.
- The invariant becomes provable by inspection instead of by argument.

(If you ever want the one genuinely useful piece — silently adding a geocoded
`GEO` to a hand-typed doctor's address so it's tappable — it's an additive,
title-preserving flag that can be switched on later. It is **off**, and it
requires no workflow change on your end. Not part of the build.)

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
adoptions(uid, calendar, icloud_uid, matched_proposal_id, score, tier,
          adopted_at, adopted_by, original_ics)
```

### Renaming on adopt

An adopted event still has its hand-typed title (`Soccer game`, `NADIA SOCCER!!`).
Decide per match whether to re-render it into the naming convention. Default to
yes — normalizing is the point — but make it a per-row toggle, since a few
hand-written titles carry detail the extraction didn't capture.

---

## 3a. Keeping appointments out of the adoption pass

Adoption is the one moment calsync looks at hand-created events at all, so it's
the one place an appointment could be mistaken for a practice — and a
mis-adopted haircut becomes a haircut calsync can later cancel.

The scoring mostly handles it already: "Nadia haircut" at 5:00 against an
ingested 5:30 soccer practice scores ~0.41 (time 0.21 + child 0.20, nothing
from sport, venue, or opponent), under the 0.50 floor. But "mostly" isn't a
safety property, so gate it explicitly.

**A keyword blocklist excludes an event from candidacy outright:** `haircut`,
`dentist`, `ortho`, `doctor`, `dr.`, `checkup`, `appt`, `appointment`,
`birthday`, `party`, `sleepover`, `recital`, `conference`, `therapy`. No model
involved — a substring match on a fixed list, running once over a bounded
window at setup. Anything it hits is out, and stays out.

Everything that survives the blocklist still has to clear the score *and* your
approval, so a false negative in the list costs nothing. That's the right
asymmetry: the list is a cheap extra floor under a human decision, not a
classifier anyone depends on.

## 4. After setup: flag duplicates, never block or delete

Once setup is done, everything without a `sync_state` row is human and stays
that way permanently. Hand-created sports events should become rare — that's
the point of the system — but someone will still type one in.

When an incoming event scores ≥0.50 against an unowned event:

> **Create it anyway, and flag it.** The review UI shows "this may duplicate an
> existing entry," with one click to either adopt the manual event (calsync
> takes it over and drops its own copy) or dismiss the flag.

Never block creation and never auto-delete the manual one. A missing event is
worse than a duplicate — same principle as `unknown` routing to Practices
rather than being withheld. A brief duplicate costs one click; a withheld game
costs a missed Saturday, and silently deleting something a person typed costs
you the family's trust in the whole system.
