# Calendar feed

**Status: specified, not built.**

A source. calsync polls a public calendar's iCal export (a Google Calendar
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

**There is no credential.** Anyone holding the URL can read the calendar. Treat
it as private all the same.

**One feed per group, many kids.** The feed is the group's, not a child's.
Which kids an event belongs to comes from the source, as with any team.

## Categories

Every distinct event title is a category: `Padawan Meeting`, `Council
Meeting`, `Elders' Roundtable`. Titles match ignoring case and surrounding or
doubled spaces. Near-duplicates (`Padawan Meeting` and `Padawan Meetings`) are
separate categories; merging them would be an alias feature, not part of this.

The source page lists every category the feed has published, with how many
events each has, and a destination for each:

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

### A new category

A category the source has never seen goes to the source's **default
calendar** at once, so a one-off is on the family's phones without waiting for
anyone. It is marked *new* on the source page, listed on `/review` until a
person confirms or changes it, and announced by **one push per new category**,
never one per poll.

This is the opposite of a sports feed, where an unknown label is held in the
`enrichment` calendar (invariant 20). There the cost of a guess is a game on
the wrong calendar; here every category already has a sensible place to go,
and a missed one-off costs more than a misfiled one.

### Changing a category

- **To another calendar:** its written events move (invariant 6: a move, never
  an update).
- **To Skip:** its events come off the calendar. They are dropped on the way to
  the write, like a withheld event, so they stay in the diff and never count as
  disappearances against the guard (invariant 2). Taking twelve meetings off
  must not look like a truncated feed.
- **Back from Skip:** an ordinary sync writes them again.

A destination is a person's decision. Nothing in the poll path sets one; the
poll only records that a category exists.

## What it writes

Every event is a non-game that keeps the feed's own label: `Luke ⚜️ Padawan
Meeting`. It takes the activity's practice alarm, gets no warm-up, and has no
opponent or home/away. All-day events follow invariant 13.

## Rules

- **Repeating series are expanded** into one event per occurrence, with
  `EXDATE` and `RECURRENCE-ID` overrides applied. Google writes rules the
  inbound expander refuses (`FREQ=MONTHLY;BYDAY=1WE`, `WKST=SU`), so this uses
  a full RRULE implementation, bounded by the sync window. An occurrence's uid
  is the series uid plus its original start, so an override replaces exactly
  one occurrence.
- **Promotion waits for events, not games** (`SyncReport.promotable`). A feed
  with no fixtures would otherwise sit on the onboarding calendar forever.
- **Change detection hashes content fields** (invariant 9): `DTSTART`, `DTEND`,
  `SUMMARY`, `LOCATION`, `DESCRIPTION`, `STATUS`.
- **An empty or unparseable feed raises** (invariant 10).

## Shape of the change

- **The mapping lives on the source, not in the adapter.** `sources.config`
  holds `categories: {label: destination}`, where a destination is a calendar
  or `skip`. Routing reads it in the sync layer (`routing.collection_for`), so
  another feed kind can opt in later without a rewrite.
- **The adapter stays pure.** It sets `Event.source_category` and reports
  categories it was not given as `new_categories`, a diagnostic that does not
  hold an event.
- **Skip joins the `off` set in `sync.py`**, the way correlated copies do
  (`claimed`), and is passed to `withheld.enforce` as `also`. It is never
  stored as `withheld`.
- **The push** reuses `enrichment.review`'s once-per-question fingerprint,
  tracked apart from the held-events one.

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

- `new_category_syncs_to_default_and_is_reported`
- `skipped_category_never_written`
- `skipping_a_written_category_removes_without_tripping_guard`
- `moving_a_category_moves_its_events`
- `recurrence_overrides_replace_one_occurrence`
- `promotable_on_events_without_games`
- `new_category_pushes_once`
