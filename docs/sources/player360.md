# Source adapter: Player360

**Shape:** `feed` · **Tier:** 2 (ICS export) · **Verified:** 2026-08-12 against a
live feed.

```
webcal://api.360player.com/v1/ics/events.ics
  ?token=<secret>&from=<unix>&group_ids=<group>
```

Publisher advertises `REFRESH-INTERVAL:PT5M` / `X-PUBLISHED-TTL:PT5M`. We don't
need 5-minute polling — 15–30 min is plenty and politer — but it signals they
expect frequent polls and are unlikely to throttle at sane intervals.

## Sample event

```
UID:360Player-event-4716716
SUMMARY:Super 8v8 Festival Rush Kickoff
DTSTAMP:20260721T145659Z
DTSTART:20260801T140000Z
DTEND:20260801T160000Z
LOCATION:Randy Custis Memorial Park 7160 Rescue Ln\, Exmore\, VA 23350
DESCRIPTION:8v8 Festival Club Kickoff (2 games)
URL:https://app.360player.com/organization/41363/events/4716716
CATEGORIES:match
LAST-MODIFIED:20260801T160002Z
CREATED:20260721T145659Z
SEQUENCE:1785600002
```

## What the feed gives us

| Need | Available? | Notes |
|---|---|---|
| `is_game` | **yes** — `CATEGORIES` | `match` → Games, `practice` → Practices |
| Stable identity | **yes** — `UID` | `360Player-event-<id>`, same id as the `URL` path |
| Real address | **yes** — `LOCATION` | free-text, inconsistently punctuated |
| Timezone | UTC only | `DTSTART`/`DTEND` are `Z`, no `TZID` |
| Opponent | **yes, for league matches** | embedded in `SUMMARY` — see below |
| Child identity | **no** | comes from the feed→child binding, not the feed |
| Cancellation | **no explicit signal** | no `STATUS`; events presumably vanish |

## Decisions this settles

**`is_game` is a lookup, not a classifier.** `CATEGORIES:match` → Games,
`CATEGORIES:practice` → Practices. No LLM, no heuristics, no `unknown` for this
source. Treat the vocabulary as open — log and route to Practices (the safe
default) on any value not yet seen, and alarm once so the mapping gets extended.

**Fuzzy matching is not needed here.** `UID` is derived from Player360's own
event id and is stable across polls, so dedup is UID equality. This keeps the
matcher on the §6a cut list.

**The title renderer earns its keep.** `SUMMARY` is generic and repeats —
three consecutive practices all read `Club Minicamp Kickoff`, and nothing names
the child. Identity comes entirely from the feed binding.

**`SUMMARY` format varies by event type, and league matches carry the
opponent.** The first sample was three preseason club events, which led me to
record "no opponent" — wrong. A regular-season league match reads:

```
U10DA TASL Match vs Beach FC U10
└─┬──┘ └─┬─┘ └─┬─┘    └────┬────┘
 team  league type      opponent
```

So `SUMMARY` is not one format but at least two, and parsing is worth doing:

```
^(?<team>\S+)\s+(?<league>\S+)\s+Match\s+vs\.?\s+(?<opponent>.+)$
```

On a match, use the captured opponent. On a miss, **strip known tokens** —
`name`, `official_name`, `aliases`, `age_group`, `league` — from `SUMMARY` and
use what remains, collapsing whitespace. Deterministic, no LLM, no cleverness:

| `SUMMARY` | strip | detail |
|---|---|---|
| `U10DA Practice` | `U10DA` | `Practice` |
| `Super 8v8 Festival Rush Kickoff` | `Rush` | `Super 8v8 Festival Kickoff` |
| `Club Minicamp Kickoff` | — | `Club Minicamp Kickoff` |

Routine practices already say `Practice` upstream, so `James ⚽️ Practice` falls
out of the same rule that produces `Patrick 🏊‍♂️ Practice` — no special case
needed, and upstream still wins whenever it says something more specific. That yields the ideal title form for league games and
degrades gracefully for club events:

```
James ⚽️ vs Beach FC        ← parsed league match
James ⚽️ Super 8v8 Festival ← unparsed, SUMMARY fallback
James ⚽️ Minicamp           ← unparsed, SUMMARY fallback
```

**Strip a redundant age suffix from the opponent** — `Beach FC U10` becomes
`Beach FC` when the suffix matches our own team's age group, which it will for
league play. Keep it when it differs, since playing up or down an age band is
information worth seeing.

`TASL` is the league; record it on the activity so it can go in `DESCRIPTION`
and help disambiguate opponents that appear in more than one league.

**Home/away comes from the venue, not the string.** Every match reads `vs`
regardless, so derive it: venue == the activity's home field → `vs`, otherwise
`@`. Wolf Trap Park hosts both the minicamp practices and the Beach FC match,
which makes it the likely home ground — set `home_venue` on the activity once
and the `@` form starts working.

**`DESCRIPTION` duplicates `SUMMARY` on league matches.** The Beach FC event
carries the identical string in both. Club events differ ("8v8 Festival Club
Kickoff (2 games)" against a different title), so the rule is: **include the
upstream `DESCRIPTION` only when it differs from `SUMMARY`.** Copying it
unconditionally puts the title in the body of every league game for no reason.

`URL` passes straight through — Apple renders it as a tappable "360Player /
Open" row above the location.

## Trap 1: `SEQUENCE` and `LAST-MODIFIED` churn on their own

`SEQUENCE` is not a small monotonic counter — it's a **unix timestamp equal to
`LAST-MODIFIED`**, verified exactly across three events:

```
SEQUENCE 1785600002 -> 20260801T160002Z == LAST-MODIFIED   DTEND 20260801T160000Z
SEQUENCE 1785884403 -> 20260804T230003Z == LAST-MODIFIED   DTEND 20260804T230000Z
SEQUENCE 1785970803 -> 20260805T230003Z == LAST-MODIFIED   DTEND 20260805T230000Z
```

Every `LAST-MODIFIED` lands **2–3 seconds after that event's `DTEND`**. Player360
touches each event the moment it finishes — closing attendance, marking it
complete, something like that.

So upstream change signals fire on events that did not change:

- **Never use `SEQUENCE`, `LAST-MODIFIED`, or `DTSTAMP` for change detection.**
  Hash the fields we actually care about — `DTSTART`, `DTEND`, `SUMMARY`,
  `LOCATION`, `CATEGORIES`, `DESCRIPTION` — into `sync_state.last_synced_hash`
  and diff on that.
- **Never propagate upstream `SEQUENCE` to iCloud.** Manage our own, or every
  event bumps its sequence as it ends and subscribers on the shared calendar
  get change notifications for games that already happened.

## Trap 2: cancellation is silent

There's no `STATUS:CANCELLED`. A cancelled event presumably just **disappears
from the feed**, which makes disappearance our only cancellation signal — and
that is dangerous against a shared family calendar.

A fetch that returns `200` with a truncated, empty, or wrong-scope body looks
identical to "the whole season was cancelled."

**Mass-disappearance guard, required before any delete path goes live:**

> If a poll shows more than ~20% of an activity's known future events missing,
> or more than 3 missing in a single poll, treat it as a **fetch anomaly**:
> cancel nothing, hold the previous state, alarm to the Matrix room, and
> require confirmation.

Also require a structurally valid `VCALENDAR` with at least one `VEVENT` before
any diff is computed. An empty-but-valid feed is not evidence of cancellation.

## Trap 3: `LOCATION` is free text with the venue name glued to the street

```
Randy Custis Memorial Park 7160 Rescue Ln\, Exmore\, VA 23350
Wolf Trap Park 1009 Wolf Trap Rd\, Yorktown VA 23692
```

Note the inconsistency: `Exmore\, VA` has a comma, `Yorktown VA` doesn't. This
is hand-entered per event, so normalize rather than trust it.

Split on the first street-number run — `^(?<name>.*?)\s+(?<addr>\d+\s+.*)$`:

| name | address |
|---|---|
| Randy Custis Memorial Park | 7160 Rescue Ln, Exmore, VA 23350 |
| Wolf Trap Park | 1009 Wolf Trap Rd, Yorktown VA 23692 |

Good news: these are **real geocodable addresses**, not "Field 4". So for this
source the venue pipeline is mostly rule-based — split, geocode, cache as an
alias, confirm the pin once. The LLM path is only for strings the regex fails
on. Still refine the pin by hand: `Randy Custis Memorial Park` is a park, and
the street address is the entrance, not the field.

## Trap 4: you are already subscribed to this feed

Apple Calendar currently has the raw feed subscribed as **"360Player Event
calendar."** The moment calsync starts writing these events into the shared
`Games` and `Practices` calendars, every Player360 event exists twice on every
family device — once from the subscription, once from calsync.

**Unsubscribing from "360Player Event calendar" is a required Phase 1 cutover
step**, not a cleanup task. Do it at the same moment writes are enabled, and
check whether anyone else in the family subscribed to it independently.

Worth noting what the subscription already does well, since calsync has to beat
it to be worth the trouble: the raw feed renders a readable title, and Apple's
data detector linkifies the street address inside `LOCATION` on its own. What
it can't do is name the child, unify swim and soccer into one place, apply
alarm policy, or drop a pin on the right corner of the park.

## Trap 5: always state venue-local time in the body

**The feed's UTC is correct — verified.** `DTSTART` 14:00Z is a 10:00 EDT
kickoff at the Yorktown venue, confirmed against the real schedule. The
screenshot showed 08:00 only because the device was in Mountain time; Apple was
correctly re-rendering the absolute instant. No systematic offset, nothing to
fix in the adapter.

But it's a standing readability problem rather than a one-off: Apple renders in
the *device's* timezone, so any family member away from Eastern sees a time
that isn't the kickoff time. Glancing at "08:00" and reasoning about when to
call, or whether it's already over, goes wrong quietly.

**Put venue-local time in `DESCRIPTION` unconditionally** — `Kickoff 10:00 ET`.
Don't try to detect travel and add it conditionally; that's state to get wrong,
and the line costs nothing when you're at home. The body is free, unlike the
title.

## Trap 6: the feed carries past events, and `from` is frozen

`from=1783895132` decodes to **2026-07-12T22:25:32Z** — about a month before it
was handed to us, and it does not move on its own.

- Store `from` as a **policy** (`now - 30d`) with the URL as a template, and
  regenerate at fetch time. Storing the literal URL means polling a fixed
  historical window forever and silently seeing nothing new.
- The feed returns events already in the past. **Bound the sync window**
  (roughly `today - 7d` forward) so a first run doesn't backfill months of
  history into a calendar the whole family reads.

## Credential handling

The token is a bearer credential **in the query string**, so it leaks into
logs, shell history, and `ps` output. Store the token in the secret store and
persist the URL as a template plus `secret_ref` — never the assembled URL.
Same treatment as the iCloud app-specific password.

## Open questions

1. **Resolved:** `group_ids=68362` is James's U10DA team, not the club. The
   one-feed-per-(child, activity) binding holds, and the generic `SUMMARY`s
   ("Club Minicamp Kickoff") are club-level *event names* landing in a
   team-scoped feed — expected, not a scoping error. It does confirm the feed
   will never identify the child; that comes from the binding.
2. **Is the token account-scoped or feed-scoped?** If account-scoped, the same
   token plus a different `group_ids` should return another kid's team — which
   would make onboarding every Player360 activity a one-credential job. Cheap
   to test: swap `group_ids` and see whether it 200s.
3. Does `group_ids` accept a comma-separated list? Even if so, **keep one feed
   per (child, activity)** — a feed bound to exactly one pairing needs no
   per-event child inference, and that's the property doing the work.
4. Full `CATEGORIES` vocabulary beyond `match` / `practice`.
5. Does the token expire?
6. Confirm cancellation behavior by watching a real one — this is the only
   trap above that's inferred rather than observed.
7. **Resolved:** `U10DA TASL Match vs Beach FC U10` is the ICS `SUMMARY` —
   confirmed in Apple Calendar rendering the subscribed feed. Opponent parsing
   is free; no API or scrape needed.
8. **Resolved:** the string always reads `vs`; there is no away marker. Derive
   home/away by venue instead (see below).
9. Full `CATEGORIES` vocabulary beyond `match` / `practice`.
10. Does the token expire?
