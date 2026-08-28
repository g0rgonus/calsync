# Source adapter: TeamReach

Status: **verified against three live feeds.** Adapter in
`src/calsync/sources/teamreach.py`, golden tests in `tests/test_teamreach.py`.
Sample payloads, all Mar–May 2026:

| Fixture | Team | Events |
|---|---|---|
| `teamreach_sample.ics` | 758329 Inter TEMPEST | 23 |
| `teamreach_otters_sample.ics` | 761305 Otters | 20 |
| `teamreach_wrens_sample.ics` | 758489 Wrens | 19 (names scrubbed — see trap 4) |

## Endpoint

```[text]
webcal://api.teamreach.net/api/events/teams/<team_id>/ical
```

`webcal://` is a subscribe hint, not a scheme — fetch it as `https://`. The
`/ical` suffix matters: `/api/events/teams/<id>` without it returns
`401 UNAUTHORIZED` ("Missing or invalid Authorization header"), which is the JSON
API rather than the calendar export.

**No credential.** The feed is unauthenticated and keyed only by team id, so
there is no `secret_ref` to configure — and equally, anyone holding the team id
can read the schedule. Treat the id itself as semi-private.

## There is no TeamReach format

This is the single most important finding, and it took three feeds to see.
Coaches enter these events by hand, and **which fields exist and what the
SUMMARY means both vary per team**:

| Team | `LOCATION` | `DESCRIPTION` | SUMMARY convention |
|---|---|---|---|
| 758329 Inter TEMPEST | ✗ | ✗ | `Game - Kingsmere #2` — type then **venue** |
| 761305 Otters | ✓ | ✗ | `Otters vs Chargers` — **us vs them**, ordered |
| 758489 Wrens | ✓ | ✓ | `Game vs Cougars` — type then **opponent** |

A parser built around any one of these silently mangles the others. Written
against 758329 first, this adapter classified **all twelve** Otters fixtures as
practices, because "Otters vs Chargers" contains neither "Game" nor "Practice".

So the adapter reads by *strategy*, not by format — try each known shape, take
whichever fires, and report the misses rather than guessing. Assume the next
team breaks a rule this table implies.

Always present across all three: `UID`, `DTSTAMP`, `DTSTART`, `TRANSP`,
`SUMMARY`, `CREATED`, `LAST-MODIFIED`, plus calendar-level
`PRODID:-//TeamReach//EN`, `METHOD:PUBLISH` and `X-WR-CALNAME` (the team name).

Never present in any of the three: `CATEGORIES`, `URL`, `STATUS`, `GEO`,
`ORGANIZER`, `ATTENDEE`, `SEQUENCE`.

## Identity: the good case

```[text]
24253410@teamreach
└─event─┘
```

Stable, unique (23/23 in the sample), no embedded timestamp. **Policy:
`passthrough`.** Contrast [flag-football.md](flag-football.md), where the UID is
regenerated every poll.

Note the earlier n8n sync table showed UIDs shaped `24611220-758329@teamreach`.
The `-758329` is the *team id, appended by that workflow* — the feed itself emits
`24611220@teamreach`. Don't key on the composite form.

## Correction: there is no `SEQUENCE`

An earlier reading of the n8n table treated its `sequence` column as a feed
field. It is not — the feed publishes no `SEQUENCE` at all. Those values
(`1773356579`, `1772412344`, …) are unix seconds, i.e. the workflow's own
`unix(LAST-MODIFIED)`.

So "key off UID and sequence" really meant "key off UID and `LAST-MODIFIED`".
That works, but `DTSTAMP == LAST-MODIFIED` on all 23 events, so both are pure
modification stamps and neither belongs in a content hash. `HASH_FIELDS` is
therefore just `DTSTART`, `DTEND`, `SUMMARY` — everything else the feed offers is
either an mtime or a constant.

## Trap 1: game vs practice has two independent signals

No `CATEGORIES`, so the type comes from the SUMMARY. Observed vocabulary across
three teams:

```[text]
Practice   First Practice   Game   First Game   Make Up Game
Playoff Game   Playoff Game2   Rescheduled Playoff Game
```

Matching is on the *word* `game`, not an enumerated list, so "Friendly Game" or
the typo "Playoff Game2" still route correctly.

But the Otters feed has **no type word at all** — `Otters vs Chargers`. So a named
opponent is the second signal: a fixture implies a game. Type word wins where
present; opponent decides otherwise. An event with neither falls to practices — a
mis-filed practice is a smaller error than a missed game — and is reported in
`unknown_types`.

## Trap 2: home/away comes from word order, but only sometimes

`Otters vs Chargers` and `Rampart vs Otters` alternate through the Otters season, so
the ordering is real information: whichever side is *us* fixes both the opponent
and home/away. That is resolved against `Activity.known_tokens()`, so it depends
on the activity being named or aliased to match what the coach types.

Two cases where it must **not** be claimed:

- `Game vs Cougars` (Wrens) — the left side is a type label, not a team. The
  summary says who, not where, so `home` stays `None`.
- Neither side matches our tokens — the fixture is recorded in `unidentified`
  and **no opponent is claimed at all**. Picking one would be a coin flip that
  could name our own team as the opposition. The fix is an activity alias, and
  the report tells you which strings need one.

This follows the same rule as `normalize/title.py`: away is marked only when
positively known.

## Trap 3: venue may be a real field or a fragment of prose

Where `LOCATION` exists it wins — a coach filled it in deliberately, whereas the
summary tail is whatever was left over after parsing. Only 758329 lacks it, and
that is the one where the venue has to come out of the summary.

`LOCATION` is not clean either. One team publishes `Kingsmere Meadow Park`,
`Kingsmere Meadow Park ` and `Kingsmere Meadow Park Soccer Fields` for what is
plainly one place. Whitespace collapse handles the first two; the third is a
different string and belongs in `venue_aliases`, not in a regex.

## Trap 4: `DESCRIPTION` can carry other families' children

The Wrens feed uses it for a snack rota — "Ana has snacks" — naming ~18
children. It is carried into the event body, because to a team parent that is
the point of the field.

It also means **feed payloads are not safe to commit unexamined**. The fixture
in `tests/fixtures/teamreach_wrens_sample.ics` has those first names replaced
with placeholders; the structure is preserved, the real names are not in git.
Scrub before adding any new sample.

## Trap 5: the SUMMARY is coach-typed, and it shows

All six of these appear in one 23-event feed:

```[text]
'Practice - Windmere '            trailing space
'Practice  - Windmere '           doubled space before the dash
'Practice- Windmere'              no space before the dash
'Game - Kingsmere#2'             missing space before #
'Playoff Game - Windmere.  6pm'   trailing time the DTSTART already carries
'Playoff Game2- Kingsmere #2'    typo
```

Normalization is deliberately shallow: collapse whitespace, close up `#`, drop a
trailing time, strip trailing punctuation. Mapping variants onto a canonical
venue is the `venue_aliases` table's job — a regex that renames venues would be
guessing.

The trailing-time pattern requires a meridiem or a `:`. A bare trailing digit is
not a time, and stripping one turns `Kingsmere #2` into `Kingsmere #`. That bug
was written, then caught by `test_summary_variants_normalize_to_the_same_fields`.

## Trap 6: one event has no DTEND

`24611220@teamreach` publishes `DTSTART` with no `DTEND`. Default behaviour
matches Player360 — the event ends when it starts — because inventing a duration
is worse than a zero-length event. `default_duration_min` lets a deployment
choose otherwise; it is off by default rather than guessing 60 minutes.

## Trap 7: cancellation is silent

No `STATUS`, so as with Player360 an event simply vanishes. The disappearance
guard in `diff.py` is the only protection, and it applies unchanged.

## Trap 8: a tournament day is published as a date, not a time

**Observed 2026-08-28.** Two events in a 21-event feed:

```
DTSTART;VALUE=DATE:20251021
DTEND;VALUE=DATE:20251022
SUMMARY:Semifinal Games
```

A coach entering a knockout day months before the bracket exists. `DTEND` is
exclusive, so that is one day. Both carried `TRANSP:TRANSPARENT` and
`X-MICROSOFT-CDO-BUSYSTATUS:FREE`, neither of which calsync reads.

They used to raise, and the raise took the whole feed with it — nineteen live
fixtures never reached the calendar because of two the adapter could not read.
`Event.all_day` now carries them: anchored at local midnight in the activity's
timezone, written back as `VALUE=DATE`, and with no alarm, since "90 minutes
before" a day is the previous evening.

## Open questions

- Does `LAST-MODIFIED` churn on untouched events the way Player360's does? Two
  polls a day apart with unchanged content would settle it. Not blocking —
  `content_hash` does not consult it.
- Is the event id globally unique or per-team? It looks global, but a second
  team's feed would confirm it before relying on it across activities.
- Whether the venue shorthands ("Windmere", "Ashgrove", "Kingsmere #2") resolve
  to schools that need `venue_aliases` rows with real addresses and pins.
