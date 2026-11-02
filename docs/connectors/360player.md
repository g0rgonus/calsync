# 360Player

A source. calsync polls a team's 360Player calendar export.

## Using it

**Get the feed URL** from the team's calendar export in 360Player. It looks
like this:

```
webcal://api.360player.com/v1/ics/events.ics?token=…&from=…&group_ids=…
```

Paste it into the console with `https://` in place of `webcal://`. calsync
fetches over http(s) only and refuses anything else.

**The token is a credential.** It sits in the query string, so the console
moves it into the secret store and keeps `{{secret:…}}` in its place. A URL
that would not reassemble exactly is refused rather than stored
(`onboarding.templatise`).

**One feed per kid per team.** `group_ids` picks the team. The feed never names
the child: which kid an event belongs to comes from the source you set up, not
from the feed. A player's own feed (`user_ids=`) works too, and also carries
guest games with other squads.

**Worth setting on the source page:**

- **Home venue.** Without it calsync cannot tell home from away on a `vs`
  fixture (see below).
- **Warm-up minutes**, if the team asks players to arrive early.
- **Persists across seasons**, for a club team kept year to year, so a quiet
  summer does not switch it off.

**If anyone already subscribes to the raw feed** in their calendar app,
unsubscribe it when calsync starts writing. Otherwise every event shows twice.

## What to know

Each item is pinned by tests in `tests/test_player360.py`. If one fails after a
feed change, read the item before changing the assertion.

### The type comes from `CATEGORIES`

`match` (and `game`, `scrimmage`, `tournament`, `friendly`) is a game;
`practice` and `training` are practices. Any other value is held in the
`enrichment` calendar until someone answers on `/review`, which adds the word
to the team's vocabulary. A guess would put a tournament on the wrong calendar,
and fixing that later is a move, not an update.

*Tests:* `is_game_comes_from_categories`, `routing_follows_is_game`

### League matches carry the opponent in `SUMMARY`

```
U10RS RSL Match vs Mos Eisley FC U10
└─team └league └type └opponent
```

- The separator is `vs`, `vs.`, `v.` or `@`; `@` needs no space after it.
- The team token is optional. A guest game on a player feed reads
  `ORSL Match vs Cloud City FC 2015/2016`.
- A trailing age band is dropped when it matches the team's own
  (`Mos Eisley FC U10` → `Mos Eisley FC`) and kept when it differs, since
  playing up or down is worth seeing.
- A trailing birth-year band (`2015/2016`, `2015/16`, `2016`) is always
  dropped.
- Anything that is not a league match keeps its own label, minus the team's
  own name and aliases: `Super 8v8 Festival Jedi Kickoff` →
  `Super 8v8 Festival Kickoff`. The upstream label always beats a generic
  "Practice".

A guest fixture's left-hand side is not evidence of the team's name, so it
never feeds the onboarding guess at it.

*Tests:* `league_match_yields_opponent_with_age_suffix_stripped`,
`practice_summary_strips_our_own_team_token`, `club_event_keeps_its_own_label`,
`a_guest_fixture_with_no_team_token_still_parses`,
`opponent_birth_year_band_is_dropped`,
`a_guest_fixture_is_not_evidence_of_our_team_name`, `titles`

### `@` means away; `vs` means nothing

The feed writes `vs` wherever the game is played, so `vs` decides nothing. `@`
is the coach saying the game is away, and it wins. Otherwise the venue decides:
at the home venue is home, and with no home venue set it stays unknown. Unknown
is never shown as away (`normalize/venue.matches_home`).

*Tests:* `home_away_derived_from_venue_not_the_vs_string`,
`an_away_match_is_read_from_the_at_marker`,
`the_at_marker_is_what_makes_it_away`, `a_vs_match_still_defers_to_the_venue`,
`away_only_marked_when_positively_known`

### `LOCATION` glues the venue name to the street

```
Theed Park 1009 Theed Rd\, Naboo NX 40114
Jedi Temple Memorial Park 7160 Senate Ln\, Coruscant\, NX 40219
```

Typed per event, with or without a comma before the state. calsync splits on
the first street-number run into name and address, and resolves no
coordinates.

*Tests:* `venue_split_on_street_number`, `no_coordinates_are_invented`

### `SEQUENCE` and `LAST-MODIFIED` churn on their own

`SEQUENCE` is a unix timestamp equal to `LAST-MODIFIED`, and 360Player touches
every event 2–3 seconds after it ends. Change detection hashes `DTSTART`,
`DTEND`, `SUMMARY`, `LOCATION`, `CATEGORIES` and `DESCRIPTION` instead, and
upstream `SEQUENCE` is never passed on. Otherwise every event would be rewritten
the evening it was played.

*Tests:* `content_hash_ignores_sequence_and_last_modified`,
`content_hash_reacts_to_a_real_change`

### Cancellations

A cancelled event stays in the feed marked `STATUS:CANCELLED`. calsync takes it
off at once, with no guard: it is a statement about that event, not an absence,
and it is left out of the guard's arithmetic. An event that vanishes from the
feed goes through the disappearance guard like any other source. An empty or
unparseable feed raises rather than reading as a cancelled season.

*Tests:* `a_cancelled_event_is_carried_as_cancelled`,
`a_confirmed_event_is_not_cancelled`, `the_status_is_not_part_of_the_content_hash`,
`a_feed_cancellation_skips_the_guard_entirely`,
`feed_cancellations_are_left_out_of_the_guards_arithmetic`,
`a_held_disappearance_does_not_hold_feed_cancellations`,
`an_untracked_feed_cancellation_is_nothing_to_do`,
`single_disappearance_is_a_cancellation`, `mass_disappearance_is_held_not_processed`,
`guard_trips_on_count_even_when_percentage_is_low`,
`diff_classifies_new_changed_and_unchanged`,
`empty_feed_is_not_evidence_of_cancellation`,
`garbage_feed_raises_rather_than_returning_nothing`

### Times are UTC

`DTSTART` and `DTEND` are always `Z`, with no `TZID`. The event body states the
start in the team's zone.

*Tests:* `times_are_utc_and_local_start_uses_venue_zone`

### `DESCRIPTION` often repeats `SUMMARY`

On league matches the two are identical. The description goes into the event
body only when it differs.

*Tests:* `description_dropped_when_it_duplicates_summary`
