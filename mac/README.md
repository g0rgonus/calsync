# calsync-mirror

Mirrors calsync's Radicale collections into the shared Apple calendars the
family already has on their phones. Runs on one Mac, under launchd, every 15
minutes.

```text
Radicale /games      ──┐                        ┌─→  Goergen Kid Activities
                       ├─→  calsync-mirror  ────┤
Radicale /practices  ──┘      (this tool)       └─→  Goergen Grandparents
```

It replaces an earlier tool that copied between two local EventKit calendars.
The reconciliation loop, the launchd job and the shape of the output are that
tool's; the source side and everything about safety are new.

## Why it is a separate program

`docs/PLAN.md` §5b draws the boundary at **agent versus human**, not at network
position. calsync's responsibility ends at Radicale: it holds no iCloud
credential, and nothing but this tool writes to the family's calendars. Keeping
it out of the Python package is what makes that true by construction rather than
by discipline — there is no code path from a poller to an EKEventStore.

## Install

```bash
./install.sh                     # build, install CLI + app, run at login
calsync-mirror --check           # can it read Radicale? no calendar access needed
calsync-mirror --dry-run         # what would it do? macOS prompts for Calendar here
```

Run those in that order. The dry run should be the first thing that touches the
calendars: it is both the run that asks for permission and the one that prints
the plan before anything is written. After that the menu bar app keeps it in
step on its own, and the CLI is there for one-offs and debugging.

## The menu bar app

`Calsync Mirror.app` runs at login, syncs on its own timer, and is the surface
for the three states that otherwise only reach a log file nobody opens:
deletions withheld, a long outage, and events waiting on a human.

```
  ✓  In sync                    no changes, last checked 4m ago
  ⏸  Paused until 08:00
  ⃠  Radicale unreachable       last read 3h ago, nothing to do
  !  Deletions withheld         6 of 21 tracked events vanished in one read
  !  3 events held, 1 answer to approve
```

It **cannot resolve anything**. Status, pause, Sync Now and links out are all
about this machine; answering a question or approving an answer happens in the
console, by a person, because that is where the review gate is
([docs/API.md](../docs/API.md)). The menu links there rather than reimplementing
it.

**Pause expires by default.** A pause you forget is the family's calendar going
quietly stale for weeks — the same failure `persists_across_seasons` prevents on
the calsync side, where switching something off in July means noticing in
September. An indefinite pause is offered, because "I am about to reorganise
this calendar by hand" is a real reason, but it is the option you choose rather
than the one you land on, and past a day the menu says how long it has been.
Resuming needs no catch-up: the reconciler re-derives everything from the
calendar.

**Notifications fire once per condition, not once per run** — the same
notify-once fingerprint `enrichment.review` uses, for the same reason. The timer
runs every fifteen minutes, and a push each time is muted by lunchtime, which is
worse than no push at all.

The CLI honours a pause too, so a stray one-off cannot write while the app is
holding.

### The review badge

`GET /v1/review` on calsync's API reports how much is waiting on a human, and
the menu shows it. Configure `apiURL` and `apiToken` to switch it on; without
them there is simply no badge and the mirror is unaffected. The console URL is
derived from the API URL rather than configured twice — the proxy puts both on
one origin, so knowing one is knowing the other.

That endpoint runs no dry run and fetches no feed, which is what makes it safe
to poll on a timer.

## Why a bundle and not an Xcode project

What makes this a real app is the **bundle**: a proper name in the permission
dialog instead of `calsync-mirror`, a bundle id (which notifications require),
`LSUIElement` so there is no Dock icon. `install.sh` assembles it around the
`swift build` output.

An `.xcodeproj` is the usual way to produce one, and it is the right answer the
moment this wants Developer ID signing or notarization. It is not the answer
yet: Core, the CLI and the 70 tests are a SwiftPM package regardless, so adding
Xcode would mean two build systems to keep in step for a tool that runs on one
laptop. Nothing is lost by waiting — the Swift code does not move when the
project file arrives.

The build is ad-hoc signed, so **macOS may ask for calendar access again after a
rebuild**. That is the cost of not having a Developer ID, not a bug.

Config lives in `~/.config/calsync-mirror/config.json` (mode 600, because it can
hold a calendar password):

```json
{
  "radicaleURL": "http://homebox:5232/calsync",
  "pairs": [
    {"collection": "games",     "calendar": "Goergen Kid Activities"},
    {"collection": "practices", "calendar": "Goergen Grandparents"}
  ],
  "windowBackDays": 30,
  "windowForwardDays": 365,
  "syncIntervalMinutes": 15,
  "apiURL": "http://homebox/v1",
  "apiToken": "…"
}
```

Every field but `radicaleURL` and `pairs` is optional and defaults sensibly, so
a config written by an older build keeps loading — a tool that refuses to start
because it gained a setting stops syncing on upgrade.

`radicaleURL` is the **principal**, not the server root — CalDAV collections live
under a user and Radicale answers 403 at the root. If the deployment runs
`CALSYNC_RADICALE_ANONYMOUS_READ=1`, no username or password is needed at all.

Only `games` and `practices` belong in `pairs`. `onboarding` and `enrichment`
are holding pens: putting an event calsync could not classify in front of the
whole family is exactly what they exist to prevent. They stay visible in
`/review` and in a calendar client pointed at Radicale.

## How it knows which events are its own

**EventKit gives no way to set an event's UID.**
`calendarItemExternalIdentifier` is read-only and iCloud assigns it on create,
and the `X-CALSYNC-UID` property on the VEVENT does not survive into an EKEvent
either. So the identity calsync already has cannot travel in the field built for
it, and the only channel left is a writable text field:

```text
Soccer · Otters (Marbury Otters U11)
Start 14:00 EDT
Field: #2
1009 Thistledown Rd, Marbury NX 40114

Managed by calsync — uid:360Player-event-4716716
```

That line is the whole ownership model. An event carrying it may be updated or
deleted; an event without one is somebody's haircut and is never touched. It
being human-readable is the second half of the point — the family can see which
entries are managed.

Because identity lives in the events themselves, **no state file holds
identity.** The old tool kept `~/.sports-calendar-sync.json` mapping source UIDs
to event ids, and losing it meant recreating every event beside itself. This
re-derives ownership from the calendar on every run.

There is one small state file, and it is deliberately of a different kind:
`~/Library/Application Support/calsync-mirror/health.json` records only when a
read last worked, which is not derivable from anywhere else. Losing it costs
nothing — the tool forgets it has been offline and starts counting again. No
decision about what gets written depends on it.

## Duplicates

New events are written and the hand-made ones are left alone, with a report:

```text
  + create  Sat 12 Sep 14:00  Jesse ⚽️ vs Harbour FC
  ! possible duplicate on Sat 12 Sep 14:00: "Soccer game" is already on the
    calendar, and was not created by calsync
    (nothing was deleted — remove the hand-made copies in Calendar.app if they
    are the same event)
```

There is no scoring cascade and no auto-adoption, because `docs/PLAN.md` §6a
lists the adoption matcher as the first thing to cut: clearing one season of
hand-typed entries takes ten minutes, and a fuzzy matcher takes a weekend to
build and is used exactly once.

Two things make the report readable, and both were forced by running it against
a real calendar rather than a fixture:

- **Time proximity, not just the day.** `docs/MATCHING.md` §2's blocking key on
  its own produced 187 warnings for 48 events, because the destination is a *kid
  logistics* calendar where "same day" means school drop-off, a swim practice
  and two pickups. A report naming `School Drop-Off` as a possible duplicate of
  a soccer practice is one somebody learns to scroll past. The band is ±60
  minutes — `MATCHING.md`'s tier-2 tuple, sized that way because hand-entered
  events often encode *arrival* time rather than start time.
- **Grouped by what is already there.** 113 separate warnings is not a report.
  Grouped it is a dozen lines, and the answer is immediate:

```
  ! 38 of these land within an hour of something already on this calendar
    that calsync did not create:
         38 × James ⚽️ Practice
         34 × Patrick 🏃‍♂️ Drylands
         19 × Amelia/James 🏊‍♂️ Practice
    Nothing was deleted. Remove the hand-made copies in Calendar.app if they
    are the same event.
```

`James ⚽️ Practice ×38` is a season somebody synced by hand; `Patrick 🏃‍♂️
Drylands ×34` is a different kid's training that merely overlaps it. A person
tells those apart instantly, which is why grouping beats scoring here.

## What stops it emptying a calendar

`CalDavTarget.cancel` is a hard DELETE, so **absence is the only cancellation
signal here too** — and a partial read, a 502, or a mistyped collection name has
the same shape as "the season was called off".

- **The same guard `diff.py` applies to a feed.** More than 3, or more than 20%,
  of tracked *future* events vanishing in one run withholds every deletion and
  exits 3. Thresholds match `diff.MAX_DISAPPEARANCE_*` on purpose; two numbers
  meaning the same thing drift apart the moment one is tuned.
- **Total turnover is held too** — nothing tracked survived and nothing arriving
  is recognised means the collection moved or the mapping is wrong, never a
  normal season.
- **Deletion only ever reaches marked events, and only forward.** A game played
  in April is history, and Radicale holds the only copy of a past season.
- **A body that is not iCalendar throws** rather than parsing as zero events,
  so an HTML error page cannot read as an empty collection.

- **A truncated read throws.** A body that starts as a calendar and stops
  partway parses fine, and the events lost off the end look exactly like
  cancellations. The disappearance guard catches a big truncation and would wave
  a small one through, so `END:VCALENDAR` is required — completeness is a fact
  about the document, not a judgement about the season.

Exit codes match calsync's: `0` ok, `1` error, `3` a guard held, plus `4` for
unreachable. A launchd job that treats 3 or 4 as a crash will retry forever.

## Off-site

The laptop leaves the network Radicale lives on, and launchd keeps firing every
fifteen minutes. Nothing is written and nothing is deleted — that was already
true, since a failed read aborts before the part that decides anything, the same
ordering `sync.py` is built around. What is handled is the rest of it:

- **Every collection is read before any calendar is opened**, so an off-site run
  never touches EventKit and never prompts for calendar access on a network
  where it could do nothing anyway.
- **It fails in ten seconds, not sixty.** A private address on a network that is
  not this one does not refuse a connection, it swallows it, and URLSession's
  default is a 60-second timeout per collection. Radicale is on a LAN or a
  tailnet: it answers quickly or it is not there.
- **It stops after the first collection.** Every pair is the same server, so
  probing the rest spends another timeout each to learn the same thing. A *404*
  does not short-circuit — that says nothing about the next collection, and
  suppressing it would let one mistyped name quietly stop the other calendar.
- **One line, and it says how long it has been.** `Radicale unreachable — last
  read 3h ago, nothing to do`. Past `offlineWarnAfterHours` (48 by default) it
  goes to stderr as a warning, because at that point "I am away from home" has
  stopped being the likely explanation. An error every fifteen minutes for a
  fortnight trains you to ignore a log that will one day say something real.
- **A captive portal is unreachability, not an error.** A coffee-shop portal
  answers `200` with an HTML login page. That fails the calendar check, and is
  reported as being offline with the portal named, rather than as a fault in
  the deployment.
- **Exit 4, never 1.** A laptop away from home is not a broken deployment.

Coming home needs no intervention: the next run reads, writes whatever changed
while away, and resets the counter.

If the deployment is reachable over Tailscale, pointing `radicaleURL` at the
tailnet name makes most of this moot — it stays reachable off-site, and this
becomes the handling for the times the tailnet itself is down.

## Version

`0.1.0`, written in exactly one place — `Sources/CalsyncMirrorCore/Version.swift`.
`install.sh` asks the built binary (`calsync-mirror --version`) rather than
carrying its own literal, so the number in `Info.plist` cannot disagree with the
number the tool reports. Same rule as `calsync.__version__`, and for the same
reason `pyproject.toml` records: two literals drifted for a whole release
without anything noticing.

It is tracked **separately from calsync's version**. This is a different program
with its own lifecycle — it mirrors whatever Radicale holds, and a shared number
would imply a compatibility relationship that does not exist.

## Tests

```bash
swift test        # 83 tests
```

`Tests/.../Fixtures/games.ics` is **generated by calsync's own
`targets/ics_file.py:to_ics`**, not written by hand — a hand-written fixture
tests the parser against the author's belief about the format, which is the
belief most likely to be wrong. It caught real things: calsync folds
descriptions mid-word, writes a 90-minute alarm as `-PT1H30M` rather than
`-PT90M`, and emits `VALUE=DATE` with an **exclusive** `DTEND` where EventKit
wants the last day inclusive.

Regenerate it after a change to calsync's serializer:

```bash
.venv/bin/python mac/Tests/CalsyncMirrorCoreTests/Fixtures/generate.py
```

## Layout

```text
Sources/CalsyncMirrorCore/     everything that decides anything — no EventKit
  ICS.swift                    parse the VEVENT subset calsync writes
  Marker.swift                 the notes line, and reading it back
  Reconcile.swift              create / update / delete / flag, as a plan
  Guard.swift                  the disappearance guard
  Radicale.swift               GET a collection
  Config.swift
Sources/calsync-mirror/        the only code that touches EventKit or argv
  CalendarStore.swift
  Main.swift
```

The split is the testing story: the layer that can delete things from a family
calendar is a pure function over plain structs, and every safety rule above has
a test that does not need a Mac, a calendar, or a permission prompt.
