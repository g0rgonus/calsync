# TeamReach

A source. calsync polls a team's TeamReach calendar export.

## Using it

**Get the feed URL** from the team's calendar subscription link in TeamReach:

```
webcal://api.teamreach.net/api/events/teams/<team_id>/ical
```

Paste it into the console with `https://` in place of `webcal://`. Keep the
`/ical` suffix: without it the same path is TeamReach's authenticated JSON API
and answers 401.

**There is no credential.** The feed is keyed only by team id, so anyone
holding the URL can read the schedule. Treat the URL as private.

**Teach it the team's name.** TeamReach feeds are typed by coaches, and the
only way to tell our side of `Rogues vs Tuskens` from theirs is a name or alias
on the activity. The console proposes one from how often each name appears; a
wrong or missing one shows up as fixtures waiting on `/review` rather than as
wrong data.

**Teach it the team's words.** If a coach's label means game or practice in a
way calsync can't tell ("Skills Session", "Scrimmage Prep"), answering it on
`/review` adds it to the team's vocabulary (`game_words`, `practice_words`).
Taught words extend the built-in ones and beat them when they disagree.

## What to know

Each item is pinned by tests in `tests/test_teamreach.py`. If one fails after a
feed change, read the item before changing the assertion.

### There is no TeamReach format

Coaches enter every event by hand, and which fields exist and what `SUMMARY`
means vary per team. Three conventions are known:

| Shape | Example | `LOCATION` |
|---|---|---|
| type, then venue | `Game - Anchorhead #2` | none |
| us vs them | `Rogues vs Tuskens` | yes |
| type, then opponent | `Game vs Krayts` | yes |
| opponent only | `vs. Kamino B`, `@ Hoth Classical`, `@Endor Academy*` | yes |

The adapter tries each shape and takes whichever fires. When none does it
reports the miss instead of guessing. Expect a team that breaks a rule this
table implies.

*Tests:* `fixture_shapes`, `this_feed_names_venues_not_opponents`,
`a_summary_with_no_separator_still_yields_a_type`,
`a_leading_type_word_is_not_mistaken_for_a_team`,
`the_other_two_conventions_still_parse`, `an_opponent_only_feed_parses_end_to_end`

### Game or practice: two signals

There is no `CATEGORIES`. A type word wins where there is one: `game` anywhere
in it, so "Playoff Game2" and "Friendly Game" are games, and `practice` or
`training` mean practice. Where there is no type word, a recognised opponent
means a game (`Rogues vs Tuskens`).

A label that is neither is held in `enrichment` until someone answers on
`/review`. An event with no label and no opponent says nothing anyone could
answer, so it goes to practices: a mis-filed practice costs less than a missed
game.

*Tests:* `practice_and_game_are_distinguished_without_categories`,
`event_type_vocabulary`, `unrecognised_type_falls_to_practice_and_is_surfaced`,
`us_vs_them_summaries_are_games_despite_no_type_word`,
`a_taught_word_classifies_an_otherwise_unknown_type`,
`operator_vocabulary_beats_the_built_in_heuristic`,
`taught_words_extend_rather_than_replace`,
`the_vocabulary_reaches_the_adapter_through_source_config`

### Home and away come from which side we are on

In `Rogues vs Tuskens` and `Scarif vs Rogues`, the side that matches the team's
name or alias fixes both the opponent and home or away. Two cases claim nothing:

- `Game vs Krayts`: the left side is a label, not a team, so the opponent is
  known and home is not.
- Neither side matches: the fixture is held on `/review` with no opponent,
  because picking one could name our own team as the opposition.

In the opponent-only shape, a leading `@` states away; a leading `vs.` names
the opponent and says nothing about where. `@` needs no space after it; `vs`
needs a dot or a space, so a team called "Ryloth Juniors" keeps its name. A
trailing `*` (meaning unknown) is stripped, so `Bespin Academy*` is not a
second school.

*Tests:* `home_and_away_come_from_which_side_we_are_on`,
`type_led_fixtures_yield_an_opponent_but_no_home_claim`,
`an_unidentifiable_fixture_claims_no_opponent`,
`a_summary_that_names_only_the_opponent_is_a_fixture`,
`a_leading_at_states_away_and_a_leading_vs_states_nothing`,
`the_at_sign_does_not_need_a_space_after_it`,
`a_trailing_asterisk_does_not_mint_a_second_opponent`,
`a_team_whose_name_starts_with_vs_is_not_eaten`

### Venues: typed by hand, every time

`LOCATION` wins when the team fills it in; otherwise the venue comes from the
summary tail. Either way it is coach-typed:

```
'Practice - Dagobah '            trailing space
'Practice  - Dagobah '           doubled space before the dash
'Practice- Dagobah'              no space before the dash
'Game - Anchorhead#2'            no space before #
'Playoff Game - Dagobah.  6pm'   trailing time DTSTART already carries
```

Normalisation is shallow: collapse whitespace, close up `#`, drop a trailing
time (only with a meridiem or a `:`, so `Anchorhead #2` keeps its 2), strip
trailing punctuation. The field designator is split off the venue
(`Anchorhead #2` is `Anchorhead`, field `#2`), but a plural name stays whole
(`Anchorhead Meadow Park Soccer Fields`). Different names for one place, such as
`Anchorhead Meadow Park` vs `… Soccer Fields`, are the alias table's job, not a
regex's. No address or coordinates are invented.

*Tests:* `summary_variants_normalize_to_the_same_fields`,
`venue_variants_collapse_to_one_name`, `field_designator_is_split_off_the_venue`,
`split_field_keeps_plural_place_names_intact`,
`location_field_is_preferred_over_the_summary_tail`,
`no_coordinates_or_address_are_invented`

### `DESCRIPTION` can carry other families' children

One team uses it for a snack rota naming other kids. It goes into the event
body, because to a parent on the team that is the point of the field, but it
means **a feed payload is never safe to commit as it is**. Rewrite every name
into the invented world first (CONTRIBUTING, "Fixtures").

*Tests:* `description_is_carried_into_the_body`

### Identity and change

The uid (`<id>@teamreach`) is stable and passed through. The feed publishes no
`SEQUENCE`, and `DTSTAMP` always equals `LAST-MODIFIED`, so both are modification
stamps. Change detection hashes `DTSTART`, `DTEND`, `SUMMARY`, `LOCATION` and
`DESCRIPTION`.

*Tests:* `uid_is_stable_and_passed_through`,
`content_hash_ignores_modification_timestamps`,
`content_hash_reacts_to_a_real_change`

### Times, missing ends and all-day events

Times are UTC. An event with no `DTEND` ends when it starts; inventing a
duration would be worse than a zero-length event. A tournament day entered
before its times exist arrives as `VALUE=DATE`. It is kept as an all-day event,
anchored at local midnight in the team's zone (UTC midnight is the previous day
west of Greenwich), with no alarm. One unreadable event names itself and the
reason; it does not take the rest of the feed with it.

*Tests:* `times_are_utc`, `missing_dtend_defaults_to_zero_length_not_an_invented_duration`,
`missing_dtend_can_be_filled_by_deployment_choice`,
`a_day_with_no_time_does_not_kill_the_feed`,
`an_all_day_event_is_anchored_at_local_midnight`,
`a_timed_event_in_the_same_feed_is_untouched`,
`an_unreadable_event_names_itself_and_the_reason`

### Cancellation is silent

TeamReach publishes no `STATUS`; a cancelled event simply leaves the feed. The
disappearance guard is the only protection. An empty or unparseable feed raises
rather than reading as a cancelled season.

*Tests:* `empty_feed_is_not_evidence_of_cancellation`,
`garbage_feed_raises_rather_than_returning_nothing`
