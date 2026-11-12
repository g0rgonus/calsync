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

## Rules, default places and bulk answers

**Status: specified, not built.**

A hand-typed club calendar does not repeat its titles. Its first production
feed had 27 categories in a year, 23 of them used once: "Den Meeting - Special",
"Pack Meeting - Christmas", "Pinewood Derby Workshop (Option 1)", "Labor Day -
No Meeting". Each was a new category for a person to answer, and none of its
events carried a `LOCATION`. Three things take most of that work away.

### Title rules

A rule sends every category whose title matches it to a calendar, or to Skip:

| Matches | Text | Goes to |
|---|---|---|
| starts with | Den Meeting | practices |
| starts with | Pack Meeting | practices |
| contains | no meeting | Skip |
| contains | Roundtable | Skip |

- **Two kinds of match, no patterns:** *starts with* and *contains*, ignoring
  case and spacing like category names do. A parent writes these on a phone; a
  regular expression is a support call.
- **Ordered, first match wins.** The source page lets a rule move up or down.
- **Stored on the source:** `rules: [{"match": "starts_with" | "contains",
  "text": …, "destination": …}]` in `sources.config`.
- **Resolved in `routing.destination()`**, not in `collection_for` alone.
  `destination()` is what `collection_for`, `skipped()` and so the sync's `off`
  set all read, so a Skip rule takes written events off the way a skipped
  category does: through `withheld.enforce`'s `also`, never counted against
  the guard (invariant 2). `is_new()` reads rules too.
- **A category placed by a rule is not new.** It is not marked *new*, not
  listed on `/review`, and sends no push. That is the point: a rule is the
  answer given once for every category it will ever match.
- **A rule never merges categories.** "Den Meeting" and "Den Meeting - Special"
  stay two rows with their own counts; the rule only routes them. Merging is
  still an alias feature (below).

Where an event goes becomes, in order:

1. staging;
2. an event held in `enrichment`. 1.1.1 moves this ahead of the category's
   destination, for the twelve-hour check (above). On `main` before it, a
   destination is returned first;
3. the category's own destination, if a person set one;
4. the first rule that matches;
5. the default calendar.

Skip, from step 3 or 4, is applied in `sync.py` as today, not by
`collection_for`. A category's own destination beats a rule, so one exception
does not need a rule of its own.

### Showing what each rule matches

Rules are only safe if their reach is visible. "contains Meeting" also catches
"Committee Meeting / Popcorn Replacement Order Due".

- **Each category row says what placed it**: *set by you*, *rule: starts
  with "Den Meeting"*, or *default (new)*. A category that more than one rule
  matches names the first, and says it also matches the others.
- **Each rule lists the categories it matches** in the sync window, with their
  event counts, and a rule matching nothing says so: it is either waiting for a
  title that has not appeared, or a typo.
- **Every change to the rules is previewed before it is saved**: adding,
  editing, moving up or down, and deleting. Each changes which rule matches
  first and so moves written events. The form posts to a preview of the table,
  and Save applies it.
- **A category a rule stops matching falls back to the default and is new
  again.** It is marked *new* and listed on `/review`, but sends no push: the
  preview named it, and the person saving has just seen it. It is recorded in
  `category_notified` when the change is saved.

### A default place by category

`DESCRIPTION` and `LOCATION` are optional in a Google calendar, and a pack
that meets in the same hall every week leaves `LOCATION` empty. An event with
no `LOCATION` takes a **default place**, set per category or per rule, chosen
from the venue table so it carries an address and a pin.

- **The feed's own `LOCATION` always wins.** The default fills a gap and never
  overrides what the calendar says.
- **Stored on the source:** `places: {category: venue_id}` and an optional
  `place` on a rule. A category's own place beats its rule's.
- **Not part of the content hash** (invariant 9 hashes what the feed said).
  Setting or changing a default re-renders the events through the receipt
  comparison in `sync.py`, as teaching a venue alias already does, and counts
  as *refreshed*.
- **Recorded as the source layer** in `event_content` (invariant 25), with the
  venue it came from, so readers see the place the calendar shows.
- A held venue question never arises from a default: it is a venue row by
  construction.

### Answering new categories at once

`/review` and the category table get **Keep all at {default}**. It clears the
*new* mark from every category the page listed as new, and nothing else.

- **It does not set a destination.** The categories are added to
  `categories_seen` in `sources.config` and stay unset. They go on following
  `default_calendar` when it changes and any rule added later. Setting each one
  to the default would silently exempt them from future rules.
- `is_new()` becomes: no destination of its own, matched by no rule, and not in
  `categories_seen`.
- It acts on the categories listed when the page was drawn, sent with the form,
  so a category that arrived since is not answered without being seen.

### Through the API

Everything above is a decision a parent makes on a phone, and most of it an
agent can make as well or better: "Pinewood Derby Workshop (Option 2)" goes
where "(Option 1)" went. The API offers it in the two forms calsync already
has, with their safeguards unchanged.

**Read**, on `GET /inbound/v1/admin/teams/{id}/review` (admin key), from the
same live dry run the source page draws:

- `categories`: each with its event count in the window, its destination,
  what placed it (`set`, `rule` with the rule's index, or `default`), the other
  rules it also matches, its default place, and whether it is new;
- `rules`: each with the categories it matches;
- `default_calendar`, and the calendars a destination may name;
- `held_times`: the twelve-hour questions, as on the source page.

**Proposals**, for an agent working the review queue (invariant 21):

- **`route_category`**, one task per new category, answered
  `{"destination": "practices" | "skip" | "<new calendar>", "place": venue_id?}`.
  Dispatched like any other task, but not a diagnostic and not a promotion
  hold. Approving writes the same `categories` entry the source page writes.
- **`confirm_time`**, already built, for a held time.
- **Approving several at once.** `/review` gets one approve button for every
  answered task of a source. A batch of one-offs becomes one decision about an
  agent's choices, not twenty about where things go.

An agent never sends a rule as a proposal. A rule decides for categories that
have not appeared yet, which is a person's call.

**Admin calls**, for a person's own tooling, each idempotent and following the
home-venue call's pattern: logged with `log_admin`, pushed unless the source is
staged (invariant 29), and returning the value it replaced so that sending it
back is the undo.

| Call | Body |
|---|---|
| `PUT /admin/teams/{id}/categories/{label}` | `{"destination": …, "place": venue_id?}`; `null` destination clears it |
| `PUT /admin/teams/{id}/default-calendar` | `{"calendar": …}` |
| `PUT /admin/teams/{id}/rules` | the whole ordered list, so order and undo are one value |
| `POST /admin/teams/{id}/rules/preview` | a proposed list; returns what each category would get and which events would move, changing nothing |
| `PUT /admin/teams/{id}/times-confirmed` | the whole list of confirmed questions |

`{label}` is URL-encoded and matched like a category, ignoring case and
spacing. The inbound contract and OpenAPI document are generated from the
route table, so these appear there with no hand-written docs (invariant 30).

### Tests to write

- `review_lists_categories_with_what_placed_them`
- `route_category_task_dispatched_per_new_category`
- `approved_route_category_writes_the_category`
- `approve_all_applies_a_sources_answers`
- `admin_category_change_pushes_and_returns_the_previous_value`
- `admin_rules_preview_changes_nothing`
- `skip_rule_removes_written_events_without_tripping_guard`
- `rule_placed_category_is_not_new`
- `hold_beats_category_and_rule`
- `reordering_or_deleting_a_rule_is_previewed`
- `category_a_rule_stops_matching_is_new_without_a_push`
- `keep_all_leaves_categories_unset`
- `rule_added_after_keep_all_routes_those_categories`
- `rule_routes_matching_categories`
- `first_matching_rule_wins`
- `category_destination_beats_rule`
- `rule_placed_category_is_not_new_and_sends_no_push`
- `changing_a_rule_moves_written_events`
- `rule_preview_lists_matches_without_saving`
- `category_row_names_what_placed_it`
- `default_place_fills_missing_location`
- `feed_location_beats_default_place`
- `setting_a_default_place_refreshes_without_a_hash_change`
- `keep_all_answers_only_the_categories_shown`

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
- **Category aliases** and merging near-duplicates. Title rules route
  near-duplicates together but keep them as separate categories.
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
