# Event naming & location normalization

Target: existing iCloud calendars already shared with family, split by event
type (`Practices`, `Games`). The calendar axis is **type**, not child — so the
title has to carry who's involved.

## Core principle

**The title is a render, not data.** `SUMMARY` is a pure function of structured
fields (`children[]`, `activity`, `kind`, `opponent`, `home_away`, `venue`),
computed at sync time and never stored as the canonical value. Change the
convention and re-render every event; the dedup key never depends on it.

---

## 1. Title format

```
SUMMARY := kids " " emoji [" " detail]
```

Segments are omitted cleanly when empty — no dangling separators.

**The venue is not in the title.** It has its own field, that field is geocoded
and tappable (§4), and Apple Calendar shows it under the title in day and list
views anyway. Repeating it would spend the scarcest thing here — horizontal
space — on something already on screen.

| Field | Rule |
|---|---|
| `kids` | One kid → full first name. Two → initials joined with `+` (`P+J`). All three → `Kids`. Multi-kid forms are **always in birth order**, so titles never flip. |
| `emoji` | One per sport: ⚽️ 🏀 ⚾️ 🏐 🏊 🎾. Disambiguates when a kid plays more than one. |
| `detail` | **Games:** `vs Opponent` (home) or `@ Opponent` (away) when the opponent is derivable — for Player360 that means parsing it out of `SUMMARY` on league matches ([sources/player360.md](sources/player360.md)); otherwise fall back to a trimmed upstream `SUMMARY`. **Everything else:** an explicit type label — `Practice`, `Photos`, `Banquet`, `Tryouts` — prefixed with the team short name only if that kid has more than one team this season. Never empty. See §5. |

### The 12-character rule

iPhone week view truncates to roughly 12–18 characters; month view shows almost
nothing. **The first 12 characters must answer "which kid, which sport."**
That's why kid comes first and emoji second.

With the venue out of the title, most events now clear that budget outright and
the rule only binds on long opponent or tournament names.

### Worked examples

| Calendar | Title | Week view |
|---|---|---|
| Games | `Jesse ⚽️ vs Northside` | fits |
| Games | `Jesse ⚽️ @ Central HS` | fits |
| Practices | `Parker 🏊‍♂️ Practice` | fits |
| Practices | `Jesse ⚽️ Practice` | fits |
| Practices | `Jesse ⚽️ Minicamp` | fits — upstream label wins |
| Practices | `P+J ⚽️ Practice` | fits |
| Practices | `Kids ⚽️ Practice` | fits |
| Games | `Jesse ⚽️ Fall Classic R1` | `Jesse ⚽️ Fall…` |

Dropping the venue means most titles still fit **without truncation** even with
the type label restored.

**When the source carries its own label, it wins** — Player360's "Minicamp" is
more specific than "Practice", so it renders as `Jesse ⚽️ Minicamp`. The generic
`Practice` is the fallback for sources that say nothing useful, which is the
normal case for a swim PDF.

Detail resolution order for games: parsed opponent → trimmed upstream
`SUMMARY` → nothing.

`vs` = home, `@` = away. Universal convention, instantly readable, costs one
character.

### Why multi-kid uses initials

`Parker+Jesse` is 13 characters before the emoji even starts, so week view
truncates to `Parker+Jame…` — **losing the sport emoji**, which is the field
doing the disambiguation work. `P+J` keeps it visible.

Single-kid events keep the full name, since that's the common case and it fits
comfortably. The small readability cost of initials lands on the rarer form,
which is the right way round.

This works because Parker, Jesse, and Mira have distinct initials. A fourth
kid whose name starts with P, J, or M breaks it — fall back to two-letter
abbreviations (`Pa+Pe`) at that point, not to full names.

### Practices say "Practice"

Every non-game event carries an explicit type label. With the venue gone
there's budget for it, and it makes the Practices calendar uniform: each entry
states what it is rather than relying on "silence means practice." That
implicit convention was the weaker half of the earlier design — it only worked
because practices outnumber everything else.

Games don't say "Game": `vs Northside` already reads as one, and it's strictly
more informative. (Say the word if you'd rather they were symmetric.)

### What's deliberately absent

- **Any ID or tag.** Machine identity goes in properties, not the title (§3).

### Emoji caveat

Emoji render fine in Apple Calendar and in any Unicode-capable client. They do
affect string search (searching "soccer" won't match ⚽️), so keep the sport
name in `DESCRIPTION`. If you'd rather have color-blocking than name-scanning,
emoji-first (`⚽️ Jesse vs Northside`) is a defensible alternative — pick one and
never mix.

---

## 2. Other fields

**`DESCRIPTION`** — everything that doesn't fit the title. Each line is only
rendered if the field is actually populated:

```
Soccer · Vanguard (U10PL)                          ← activity config
Kickoff 10:00 ET                               ← venue-local time, always
8v8 Festival Club Kickoff (2 games)            ← upstream DESCRIPTION,
                                                 only if it differs from SUMMARY
Kit: white                                     ← relay/manual only (see below)
Arrive: 13:15                                  ← relay/manual only
Source: Player360 feed, 2026-08-09             ← provenance
Manage: https://calsync.<tailnet>.ts.net/events/3f9c1a2e
```

**Venue-local time is always stated**, because Apple renders events in the
device's timezone. Anyone reading the calendar from another timezone otherwise
sees a time that isn't the kickoff — quietly misleading rather than obviously
wrong.

**Kit and arrival time are not in any feed.** Player360 publishes neither
([sources/player360.md](sources/player360.md)), and no ICS source is likely to.
They can only arrive through the relay path — a coach message pasted into the
Matrix room — or manual entry, so treat them as optional event fields that are
usually absent, and never render an empty label.

Both are worth having as first-class fields rather than free text, because
"which jersey" is a genuine morning-of question and an amendment can set it
across a date range in one message ("white kit for all home games in October").
Keep them out of the title: the budget is tight, and the answer matters on the
morning rather than at a glance a week out.

**`URL`** — link back to the calsync event page. Apple Calendar renders it as a
tappable link, which gives you a one-tap path to the source PDF when something
looks wrong.

**`VALARM`** — from the per-activity alarm policy (§E3 in PLAN.md), not
hardcoded. Practices 30 min; away games 90 min.

---

## 3. Identity in a shared, human-populated calendar

These calendars already contain events your family created by hand. That makes
the "never touch what we didn't create" invariant **critical**, not just good
hygiene — a sync bug here deletes your spouse's entries.

- Authoritative mapping is the local `sync_state` table:
  `calsync_uid → (calendar, icloud_uid, etag, last_hash)`. Only ever `PUT` or
  `DELETE` a UID present in that table.
- Also stamp `X-CALSYNC-UID` on the VEVENT — but treat it as a **secondary**
  signal only. If a family member edits the event in Apple Calendar, the client
  may rewrite the VEVENT and drop unknown `X-` properties. Never rely on it for
  ownership.
- Ship `--dry-run` printing the exact create/update/delete set, and run it
  against the real calendars for a week before enabling writes.

---

## 4. Location normalization

Goal: a `LOCATION` that Apple Calendar renders as a map with travel-time
alerts and one-tap navigation.

### What Apple actually needs

A plain string like `Field 4` won't geocode. The reliable recipe is three
properties together:

```
LOCATION:Brookvale Park — Field 4\, 1200 Brookvale Dr\, Springfield\, IL 62704
GEO:39.781700;-89.650100
LOCATION:Brookvale Park, 41 Brookvale Dr, Halden VA
 :geo:39.781700,-89.650100
```

`LOCATION` carries the venue name and its street address. That is enough for
coordinates while showing a friendly title, which is the difference between
"navigate to the park" and "navigate to the right parking lot."

### Coordinates matter more than the address

For youth sports this is the whole ballgame. `Field 4` is a specific corner of
a large park, and geocoding the park's street address routes you to the main
entrance — frequently the wrong side, ten minutes of walking with a folding
chair. **Store a human-refined lat/lon per venue**, not whatever the geocoder
returned. The web UI should let you drag the pin once; you'll visit each venue
a dozen times a season.

### Pipeline — and what the LLM should and shouldn't do

```
raw string  →  alias lookup  →  hit?  →  done (no AI, no API call)
"RP Field 4"     (fuzzy)         miss
                                  ↓
                        LLM: parse + disambiguate
                        "Brookvale Park", sub="Field 4",
                        city from league context
                                  ↓
                        Geocoding API: name+address → lat/lon
                                  ↓
                        human confirm + drag pin (once)
                                  ↓
                        venue row + aliases, cached forever
```

**Do not let an LLM emit coordinates.** Hermes and Grok will both produce
confident, plausible, wrong lat/lon. Use a real geocoder — Apple MapKit JS
(agrees with what Apple Calendar renders, needs a developer account), Google
Geocoding, or Mapbox. Nominatim is free but weak on US sports facilities.

**Do use an LLM for the part it's good at:** turning `RP4` /
`Brookvale #4` / `Brookvale Pk Fld 4` into a structured venue guess, and using
league context to resolve `Central HS` to the right one of four in the metro.
This is genuine disambiguation and rule-based parsing fails at it.

**Where SuperGrok earns its place:** its live search is genuinely useful for
"what and where is Northside United's home field" — an obscure facility name
with no address in the source document. Route unresolved venues to a
search-capable model; route straightforward parsing to local Hermes. Both are
fine, and the split matters more than which model.

### Venue table as a cache

```
venues(id, canonical_name, short_name, address, lat, lon,
       pin_confirmed_by_human, geocoder, geocode_confidence)
venue_aliases(venue_id, alias, source)
```

Aliases are checked first, so the steady state is **zero AI calls and zero
geocoder calls** — a season has maybe 20 venues, each resolved once. New
strings hit the pipeline, land in review, and become aliases. Cost and latency
stay near zero after the first few weeks.

Unresolvable venues get the raw string in `LOCATION` with no `GEO`, and a flag
in the review queue. A non-clickable location beats a wrong pin.

---

## 5. Calendar routing

Where a feed states the type, routing is a lookup, not a classifier —
Player360 gives `CATEGORIES:match` / `CATEGORIES:practice`
([sources/player360.md](sources/player360.md)). Treat such vocabularies as
open: route unseen values to Practices, the safe default, and alarm once so
the mapping gets extended.

Routing is a single predicate:

```
calendar := is_game ? "Games" : "Practices"
is_game  := kind in (game, scrimmage, tournament)
```

Everything else — practice, training, clinic, photos, banquet, tryouts,
parent meeting, unknown — goes to Practices.

`kind` stays as descriptive metadata (it drives titles and alarm policy), but
routing only ever reads `is_game`.

### Why this matters more than it looks

**`unknown` now has a safe default.** An unclassified event lands in Practices,
which is at worst a wrong-colored entry on a calendar everyone can still see.
That's a visible, self-correcting error, not a missed event. So classification
**must not block delivery** — flag it in the review UI, but ship it.

**The classifier can be mostly rule-based.** With a binary target, presence of
an opponent is a near-perfect signal for `is_game`. Rules first, LLM only on
ambiguity.

### Implementation note: reclassification is a move, not an update

Collections are separate CalDAV URLs, so changing an event from Practices to
Games is **delete-then-create**, and the iCloud UID changes. `sync_state` needs
an explicit move operation:

```
if desired_calendar != sync_state.calendar:
    DELETE old_calendar/old_uid      # real delete, not a tombstone
    PUT    new_calendar/new_uid
    update sync_state
```

A tombstone would be wrong here — the event isn't cancelled, it moved. Getting
this wrong leaves a ghost copy in the old calendar, which is exactly the
duplicate-in-a-shared-calendar failure this system exists to avoid.

### Titles in the Practices bucket

Since Practices now holds heterogeneous events, the `detail` segment carries
the type whenever it *isn't* a routine practice:

| Event | Title |
|---|---|
| Routine practice | `Parker 🏊‍♂️ Practice` |
| Team photos | `Jesse ⚽️ Photos` |
| Banquet | `Jesse ⚽️ Banquet` |
| Tryouts | `Jesse ⚽️ Tryouts` |

Every entry names its own type — no implicit cases to remember.

### Multi-kid events

**One event listing both kids**, not one per kid. Duplicates in a shared
calendar are worse than an imperfect title.
