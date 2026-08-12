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
SUMMARY := kids " " emoji [" " detail] [" · " venue_short]
```

Segments are omitted cleanly when empty — no dangling separators.

| Field | Rule |
|---|---|
| `kids` | First names. Multiple → joined with `+`, **always in a fixed order** (birth order), so titles are stable. 3+ or whole-family → `Family`. |
| `emoji` | One per sport: ⚽️ 🏀 ⚾️ 🏐 🏊 🎾. Disambiguates when a kid plays more than one. |
| `detail` | Games: `vs Opponent` (home) or `@ Opponent` (away). Practices: team short name, **only if** that kid has more than one team this season. |
| `venue_short` | Canonical short name from the venue table — `Riverside #4`, not the street address. Address goes in `LOCATION`. |

### The 12-character rule

iPhone week view truncates to roughly 12–18 characters; month view shows almost
nothing. **The first 12 characters must answer "which kid, which sport."**
That's why kid comes first and emoji second. Everything after `·` is a bonus
that only appears in day and list views.

### Worked examples

| Calendar | Title | Truncated (week view) |
|---|---|---|
| Games | `Nora ⚽️ vs Northside · Riverside #4` | `Nora ⚽️ vs No…` |
| Games | `Jack 🏀 @ Central HS · Central HS Main` | `Jack 🏀 @ Cen…` |
| Practices | `Nora ⚽️ · Riverside #4` | `Nora ⚽️ · Riv…` |
| Practices | `Nora+Jack ⚽️ · Meadow Turf` | `Nora+Jack ⚽️…` |
| Games | `Nora ⚽️ Fall Classic R1 · Tournament Complex` | `Nora ⚽️ Fall…` |

`vs` = home, `@` = away. Universal convention, instantly readable, costs one
character.

### What's deliberately absent

- **The word "Practice" or "Game."** The calendar name and its color already
  say it. Spending 9 characters of a 12-character budget restating the calendar
  is the most common way these naming schemes fail.
- **Any ID or tag.** Machine identity goes in properties, not the title (§3).

### Emoji caveat

Emoji render fine in Apple Calendar and in any Unicode-capable client. They do
affect string search (searching "soccer" won't match ⚽️), so keep the sport
name in `DESCRIPTION`. If you'd rather have color-blocking than name-scanning,
emoji-first (`⚽️ Nora vs Northside`) is a defensible alternative — pick one and
never mix.

---

## 2. Other fields

**`DESCRIPTION`** — everything that doesn't fit the title:

```
Soccer · U12 Riverside FC
Arrive 13:15 (45 min before kickoff)
Uniform: white
Source: TeamReach post, 2026-08-09 · confidence 0.91
Manage: https://calsync.<tailnet>.ts.net/events/3f9c1a2e
```

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
LOCATION:Riverside Park — Field 4\, 1200 Riverside Dr\, Springfield\, IL 62704
GEO:39.781700;-89.650100
X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE="Riverside Park — Field 4";
 X-ADDRESS="1200 Riverside Dr\nSpringfield IL 62704";X-APPLE-RADIUS=72
 :geo:39.781700,-89.650100
```

`X-APPLE-STRUCTURED-LOCATION` is what Apple's own clients write. It pins exact
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
                        "Riverside Park", sub="Field 4",
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
`Riverside #4` / `Riverside Pk Fld 4` into a structured venue guess, and using
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

With calendars split by type, every event needs a `kind` before it can be
routed:

| `kind` | Calendar |
|---|---|
| `game`, `scrimmage`, `tournament` | Games |
| `practice`, `training`, `clinic` | Practices |
| `team_event`, `photos`, `banquet`, `tryout` | **open question** |

Multi-kid events are **one event listing both kids**, not one per kid —
duplicates in a shared calendar are worse than an imperfect title.

Open: where do non-practice, non-game team events go? Options are a third
shared calendar, folding them into Practices, or leaving them out entirely.
