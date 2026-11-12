# Calendar feed

A source, kind `calendar-feed`. calsync polls a public calendar's iCal export (a Google Calendar
public address, or any `.ics` URL) and routes each event to a calendar by its
category, which a person sets once per category.

This is for schedules that are not fixtures: a club, a troop, a school. They
have no games, no opponents and no home or away, so the game/practice split
that drives sports feeds has nothing to read. The category is the only routing
signal these feeds carry.

## Using it

**Get the feed URL.** For a Google Calendar that a site embeds, take the `src`
from the embed and build the public address:

```
https://calendar.google.com/calendar/ical/<calendar_id>/public/basic.ics
```

Onboarding preselects this kind for a Google Calendar export (its `PRODID`),
and names the source `cal-…`.

**There is no credential.** Anyone holding the URL can read the calendar. Treat
it as private all the same.

**One feed per group, many kids.** The feed is the group's, not a child's.
Which kids an event belongs to comes from the source, as with any team.

**Persists across seasons by default.** A club or school calendar goes quiet
over the summer, and `seasonend.py` switches off a fetched source after two
quiet months. Onboarding this kind sets `persists_across_seasons` in the
source's config, and the source page can clear it for a group that really does
end.

**Set the default calendar** (below) when onboarding. It starts as the
calendar this team's non-game events already go to.

## Categories

Every distinct event title is a category: `Padawan Meeting`, `Council
Meeting`, `Elders' Roundtable`. Titles match ignoring case and surrounding or
doubled spaces. Near-duplicates (`Padawan Meeting` and `Padawan Meetings`) are
separate categories; merging them would be an alias feature, not part of this.

The source page lists every category with events in the sync window, with how
many each has, and a destination for each. A category whose events are all in
the past is not listed: nothing will be written for it, so there is nothing to
decide.

| Category | Events | Goes to |
|---|---|---|
| Padawan Meeting | 40 | Explorers ▾ |
| Group Meeting | 6 | Explorers ▾ |
| Council Meeting | 12 | Skip ▾ |
| Elders' Roundtable | 8 | Skip ▾ |
| Naboo Sunday *(new)* | 1 | Explorers ▾ |

The choices are the calendars calsync already writes to, **Skip**, and **New
calendar…**. A select per row rather than drag and drop: the same model, it
works on a phone, and it needs no front-end library.

### Where an event goes

`routing.collection_for`, in order:

1. **The staging calendar, while the source is being onboarded.**
   `staging_collection` wins over everything, as for every source. Nothing from
   this feed reaches a family calendar before promotion, whatever its
   category says.
2. **Skip** keeps the event off every calendar (below), the staging calendar
   included: a person has already said nobody goes.
3. **The category's destination**, if a person has set one.
4. **The source's default calendar**, for a category nobody has set yet.

The default is `default_calendar` in `sources.config`. Onboarding sets it to
the collection this team's non-game events already render to under
`collection_template`, and the source page can change it. Changing it moves
every event in an unset category, as any collection change does.

### A new category

A category the source has never seen goes to the default calendar on the next
sync after promotion, so a one-off reaches the family's phones without waiting
for anyone. Before promotion it waits on the staging calendar with everything
else.

It is marked *new* on the source page and listed on `/review` until a person
confirms or changes its destination. This is the opposite of a sports feed,
where an unknown label is held in the `enrichment` calendar (invariant 20).
There a guess puts a game on the wrong calendar; here every category already
has a sensible place to go, and a missed one-off costs more than a misfiled
one.

**New categories are not a diagnostic.** They are not in
`PollResult.diagnostics`, do not make `is_clean` false, and do not hold
promotion. On the first poll every category is new; if they held promotion,
the source could not leave staging until each one was answered.

### Telling someone

A new category sends **one push per category**, never one per poll. The
categories already announced are kept on a column of their own,
`sources.category_notified` (a JSON list, since one push per category needs to
know which were sent), so it and the held-events announcement never silence
each other. A category that stops being new drops off the list.

While the source is staged nothing is pushed, and its categories are recorded
as announced: the person onboarding it is looking at the table, and the first
poll would otherwise send a push for every category.

The copy says what already happened, because the event is already on a
calendar:

```
Explorers: new category "Naboo Sunday"
1 event added to the Explorers calendar. Keep it there, move it, or skip it.
```

The link opens the source page's category table. On `/review` new categories
are their own section, apart from the questions about held events, with the
same wording: *already on {calendar}, confirm or change*. Neither may reuse the
held-events copy, which says nothing has gone to the family's calendars.

### Changing a category

- **To another calendar:** its written events move (invariant 6: a move, never
  an update).
- **To Skip:** its events come off the calendar. They join the sync's `off`
  set and are passed to `withheld.enforce` as `also`, the way correlated copies
  are (`claimed`). They stay in the diff and never count as disappearances
  against the guard (invariant 2), so taking twelve meetings off cannot look
  like a truncated feed. They are never stored as `withheld`.
- **Back from Skip:** an ordinary sync writes them again.

A destination is a person's decision. Nothing in the poll path sets one; the
poll only reports which categories exist.

### A time twelve hours off

Calendars are typed by hand, and AM for PM is the slip that costs something: a
Den Meeting at 06:30 rings an alarm before dawn and is missed that evening. A
timed event exactly twelve hours from its category's usual start (the most
common start, seen at least three times) waits in `enrichment` (invariant 20)
and is asked about on the source page and `/review`, under **Times look
right**, which takes the place of "Types known" for this kind. Skipped
categories are not checked.

**This time is right** stores the question itself in `times_confirmed`, so an
edit to that event's time is asked about again. If the calendar is corrected,
the question goes away and the event is released on the next sync. Like any
held event, it holds promotion until answered.

## What it writes

Every event is a non-game that keeps the feed's own label: `Luke ⚜️ Padawan
Meeting`. It takes the activity's practice alarm, gets no warm-up, and has no
opponent or home/away.

- **`LOCATION`** becomes the venue, through the same venue parsing and lookup
  as other connectors. An unknown place is an `unresolved_venues` diagnostic,
  which holds promotion like any source's, and a map pin rather than a
  calendar once promoted (invariant 20).
- **`DESCRIPTION`** goes in the body, as other connectors carry it.
- **All-day events** follow invariant 13.

## Rules

- **Change detection hashes content fields** (invariant 9): `DTSTART`, `DTEND`,
  `SUMMARY`, `LOCATION`, `DESCRIPTION`. For an occurrence the times hashed are
  its own, so editing one occurrence changes that one only. `STATUS` is not hashed. A
  `STATUS:CANCELLED` event sets `Event.cancelled` and is honoured without a
  guard (invariant 4).
- **An empty or unparseable feed raises** (invariant 10).
- **Promotion waits for events, not games, for this kind only.** The kind is in
  a set the sync reads (beside `sources.PUSHED`), and `promotable` asks for
  `events_seen > 0` for kinds in it, as it already does for pushed sources.
  Every other fetched kind keeps `fixtures_seen > 0`: a sports feed with only
  practices has not exercised the opponent path.

## Repeating series

Each series is expanded into one event per occurrence, because everything
downstream works one event at a time (`inbound/recurrence.py` explains why).
Google writes rules that expander refuses (`FREQ=MONTHLY;BYDAY=1WE`,
`WKST=SU`), so this kind uses `dateutil.rrule`, which `icalendar` already
depends on, limited to a stated subset.

**Supported:** `FREQ` of `DAILY`, `WEEKLY`, `MONTHLY` or `YEARLY`; `INTERVAL`;
`BYDAY` with or without an ordinal (`1WE`, `-1FR`); `BYMONTHDAY`; `BYMONTH`;
any `WKST`; `UNTIL`, `COUNT`, or neither. Also `EXDATE`, and `RECURRENCE-ID`
overrides of a single occurrence.

**Refused:** `BYSETPOS`, `BYHOUR`, `BYMINUTE`, `BYSECOND`, `BYWEEKNO`,
`BYYEARDAY`, `RDATE`, `RANGE=THISANDFUTURE`, and `FREQ` finer than `DAILY`. A
refused rule raises `FeedError` naming the event and the rule part, rather than
being approximated: a rule read half-right puts meetings on days nobody is
there.

**Bounded by the sync window.** The sync passes its window edges to the parser,
a new optional argument to `sources.parse`. Only occurrences inside the window
are produced, so a rule with no end produces a bounded list. The cap is 400
occurrences per series, as in the inbound expander. Past that, the parse
raises.

**Expanded in wall-clock time.** Occurrences are computed in the series'
`DTSTART` zone (its `TZID`) and only then anchored to an instant, so a 19:00
meeting stays at 19:00 across a daylight-saving change. A floating time (no
`TZID`, no `Z`) takes the calendar's `X-WR-TIMEZONE`. With neither, the parse
raises (invariant 12). An all-day series is anchored at local midnight in the
team's zone (invariant 13).

**Occurrence uids.** `<series UID>/<original start>`, where the original start
is the occurrence's start as the rule produced it, before any override:
`YYYYMMDDTHHMMSSZ` in UTC for a timed series, `YYYYMMDD` for an all-day one. A
`RECURRENCE-ID` override keeps the uid of the occurrence it replaces, so moving
one meeting is an update, not a delete and a create. A VEVENT with no `RRULE`
keeps its own `UID` unchanged. The `UID` written keeps the `/`; the CalDAV
resource name swaps it for `_` and adds a short hash of the whole uid, so it
cannot collide with a uid that already had an `_` there.

An override whose series is not in the feed is still keyed by its
`RECURRENCE-ID`, so its uid holds when the series appears.

## Shape of the change

- **The mapping lives on the source, not in the adapter.** `sources.config`
  holds `categories: {label: destination}` (a calendar or `skip`) and
  `default_calendar`. Routing reads them in `routing.collection_for`, after the
  staging override, so another feed kind can opt in later without a rewrite.
- **Left out of cross-team merging** (`correlate.py`). A merged event is filed
  by the collection template, which would undo the category's destination.
- **The adapter stays pure.** It sets `Event.source_category` and returns every
  category it saw with a count in a new `PollResult.categories`, not in
  `diagnostics`. The sync compares that with the config to find new ones.
- **One migration**, adding `sources.category_notified`.

## Not in scope

- **Sports feeds.** Their game/practice split also sets alarms, warm-ups and
  home/away, and their unknown labels are held on purpose. Opting them in to
  category routing is a later decision.
- **Category aliases** and merging near-duplicates.
- **Filtering by anything but the title** (location, description, time).

## Tests to write

Fixtures from the invented world, keeping the shape of a real Google export:
`VALUE=DATE` all-day events, a weekly series with `WKST=SU` and two
`RECURRENCE-ID` overrides, a monthly `BYDAY=1WE` series, events with and
without `LOCATION`, and no `DESCRIPTION`.

- `new_category_waits_on_staging_before_promotion`
- `new_category_goes_to_default_after_promotion`
- `new_categories_do_not_block_promotion`
- `promotable_on_events_without_games`
- `sports_feed_still_needs_a_fixture_to_promote`
- `skipped_category_never_written`
- `skipping_a_written_category_removes_without_tripping_guard`
- `moving_a_category_moves_its_events`
- `cancelled_status_is_honoured_and_not_hashed`
- `recurrence_override_keeps_occurrence_uid`
- `recurrence_keeps_wall_clock_across_dst`
- `unbounded_series_stops_at_window`
- `refused_rule_part_raises`
- `new_category_pushes_once_with_its_own_copy`
- `onboarding_sets_persists_across_seasons`
