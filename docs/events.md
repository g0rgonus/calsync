# What calsync writes

Every event calsync writes is rendered from stored fields, never copied from a
feed. Change a setting below and every event re-renders on the next sync, with
no re-fetch. Settings are on the console's `/settings` page; each one can be
seeded from `.env` as `CALSYNC_SETTING_<KEY>`.

## Titles

```
{kids} {emoji} {detail}
```

| Event | Title |
|---|---|
| Home game | `Luke ⚽️ vs Rimward` |
| Away game | `Luke ⚽️ @ Rimward` |
| Practice | `Leia 🏊 Practice` |
| Upstream label | `Luke ⚽️ Super 8v8 Festival Kickoff` |
| Two kids | `Lu+Le ⚽️ Practice` |
| Every kid | `Kids ⚽️ Practice` |
| Warm-up | `Luke ⚽️ vs Rimward warm-up` |

**Kid, then sport, first.** A phone's week view shows roughly the first 12
characters of a title, and those have to answer "which kid, which sport". The
emoji tells two sports of one kid apart, and each sport's emoji is set on the
console's `/household` page.

**Two kids are initials, joined in birth order.** Full names push the emoji
out of the week view (`Ahsoka+Luke` is 11 characters before it starts), and a
fixed order means a title never flips between `Lu+Le` and `Le+Lu`. Initials are
unique within a household: the console refuses a duplicate, so two kids who
share a first letter are given two letters each (`Lu`, `Le`). At `all_kids_threshold` kids, the title uses `all_kids_label`.

**`vs` unless away is known.** `@` appears only when the feed or the venue
states the game is away. Some feeds write `vs` for every fixture, so an unknown
is shown as `vs` and never guessed as `@`.

**The upstream label wins.** A non-game event keeps the feed's own label
(minus the team's name) when it has one, and falls back to "Practice" only when
the feed says nothing more specific.

**No venue.** The location field shows it, and the title has no room.

| Setting | Default | |
|---|---|---|
| `title_template` | `{kids} {emoji} {detail}` | Also `{sport}`, `{activity}`, `{venue}`. Empty fields collapse. |
| `warmup_title_template` | `{kids} {emoji} {detail} warm-up` | Its own template, so a warm-up always names its game. |
| `multi_kid_style` | `initials` | `names` gives `Luke+Leia`. |
| `all_kids_label` | `Kids` | |
| `all_kids_threshold` | `3` | |
| `home_marker`, `away_marker` | `vs`, `@` | |

## Body

Each line appears only when there is something to say:

```
Soccer · Jedi (U10RS)           sport · team (league name, if different)
Start 10:00 EDT                 always, in the team's zone
Game starts 10:45               on a warm-up
8v8 Festival Club Kickoff       the feed's description, if it adds anything
Kit: white
Arrive: 10:00
Field: #2
1009 Theed Rd, Naboo NX 40114
Source: p360-luke-jedi
```

The start time is always stated in the team's zone, because calendar apps show
the device's zone, and someone travelling would otherwise read a time that
isn't the kick-off. An all-day event says `All day — no time published yet`.

## Location

`Venue Name, Street Address`, which a maps app turns into a tappable,
navigable destination. No coordinates are written. The field (`#2`) is kept out
of the location and goes in the body, so one park is one place on the map,
however many fields it has.

## Which calendar

| Setting | Default | |
|---|---|---|
| `collection_template` | `{type}` | `{child}` for one calendar per kid, `{child}-{type}` for both, or a fixed name for one calendar. Also `{sport}`, `{activity}`. |
| `collection_game_label`, `collection_practice_label` | `games`, `practices` | What `{type}` becomes. |

Two more calendars hold events that are not ready for the family:

- **`onboarding`**: everything from a team that has not been promoted yet, so
  a new feed can be checked on a phone before it reaches anyone else.
- **`enrichment`**: an event calsync cannot place (an unknown event type, a
  fixture where neither side is the team), until someone answers on `/review`.

Changing an event's calendar is a delete in one and a create in the other, so a
promotion or an answer moves events without leaving a copy behind.

## Alarms and warm-ups

Each team has a game alarm and a practice alarm (90 and 30 minutes by default).
An all-day event gets none: before midnight is the previous evening.

**Warm-up minutes** on a team put a warm-up before every timed game. It ends at
kick-off, at the same venue, goes in the practices calendar and takes the
game's alarm, since it is what you leave the house for. The game's body gains
an `Arrive:` line.

---

Pinned by `tests/test_configurable.py` (a different household, configured
end to end), `tests/test_warmup.py`, and the title tests in
`tests/test_player360.py`.
