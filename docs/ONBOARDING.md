# Onboarding a feed

Rec-league teams are recreated every season with new names and new feed ids, so
onboarding is not one-time setup — it is the **recurring operational work** of
this system, several times a year, once per kid per sport. James's Rush club team
(Player360) is the exception: persistent across seasons, configured once.

Everything here optimises for that. Setup screens you use once can be clumsy;
this one gets used every spring and every autumn.

## 1. Three things only you can supply

1. **The feed URL** — from the app or the coach. Nothing can derive it.
2. **Which kid** — the feed has no idea.
3. **Which sport** — inheritable from that kid's previous season, so a confirm.

Everything else is read out of the feed.

## 2. What the feed already tells us

Measured across four real feeds:

| | Player360 / Rush | Hurricanes | Hawks | Comets |
|---|---|---|---|---|
| Team name (`X-WR-CALNAME`) | ✗ generic | `Inter HURRICANES` | `Hawks Spring 2026` | `Comets` |
| Season bounds (min/max `DTSTART`) | ✓ | ✓ | ✓ | ✓ |
| Our own team token | n/a | n/a | `Hawks` | n/a |
| Venues | ✓ | ✓ | ✓ | ✓ |

`X-WR-CALNAME` names the team on every TeamReach feed. Player360's is generic,
which matters little — that feed is configured once.

**The team token falls out of frequency.** In a "us vs them" feed our own name
appears in every fixture while each opponent appears once or twice: the Hawks
feed gives `Hawks` 12, `Strikers` 2, `Lightning` 2. Propose the top token, let
the operator correct it.

That token is load-bearing. `Activity.known_tokens()` is what resolves
`Hawks vs Strikers` into an opponent and a home/away flag, so a wrong team name
does not produce wrong data — it produces *no* opponent, reported through
`PollResult.unidentified`. See [sources/teamreach.md](sources/teamreach.md).

## 3. Venues are stable even though teams are not

Team names churn every season; the parks and schools do not. All three observed
TeamReach teams share venues — Riverview Farm Park, Passage, Sanford, Menchville,
Stoney Run.

So venue mapping is a one-time cost per venue that amortises to zero. After a
season or two the alias table covers the league and new seasons need no venue
work at all. This is also the only place a model is worth invoking, and only for
a genuinely new venue, once, written back as an alias.

## 4. Stage to an onboarding calendar

A new feed syncs to a dedicated `onboarding` collection rather than to
`games`/`practices`.

The point is **seeing it in a real calendar client on your phone**. The
12-character rule in [NAMING.md](NAMING.md) is about week-view truncation, and no
amount of reading `.ics` text tells you whether a title survives it.

Promotion is `calsync promote <source>`, which clears `staging_collection`. The
next sync relocates every event into the real calendars — no duplicates, no
cleanup, no re-fetch.

That needs two mechanisms, and only the first was free:

1. A collection change is a delete-then-create rather than an update
   (`targets.move_required`, honoured by both the CalDAV and ics_file targets),
   and `event_state` records which collection each event is in.
2. **The sync loop has to notice.** `diff_poll` compares content hashes only, so
   after promotion an unchanged feed yields nothing but `unchanged` events and
   nothing is written at all — the staged copies would just sit there. So the
   loop checks *placement* separately: for every unchanged event, recompute the
   collection it belongs in and re-write it if that differs from where it is.

The second was missing and is the reason `test_promotion_moves_events_rather_
than_duplicating` exists. It also generalises: changing `collection_template`
from `{type}` to `{child}` now relocates the whole calendar on the next sync
instead of quietly stranding every existing event.

## 5. Probation, not a wizard

**Coaches publish practices first and add the game schedule later.** At
onboarding a feed may contain nothing but practices, which means:

- Frequency-based team-token detection has nothing to work with; the name must
  come from `X-WR-CALNAME` or from you.
- **Fixture parsing cannot be validated at all**, because there are no fixtures.

So staging is not a preview you dismiss at the end of a wizard. A source stays in
the onboarding collection until real games appear and parse correctly — possibly
weeks later.

### The promotion gate

Promote when, over the events present:

- `unidentified` is empty — every fixture matched our team on exactly one side
- `unknown_types` / `unknown_categories` are empty — every event classified
- every venue resolves to a known venue row
- at least one fixture has been seen, so the fixture path has actually been exercised

All four are computed and reported: `SyncReport.diagnostics` carries the adapter's
gaps plus `unresolved_venues`, `fixtures_seen` counts games, and
`SyncReport.promotable` is the gate itself. `calsync promote` refuses unless it
passes, and says which condition failed. `--force` overrides.

## 6. Where AI belongs, and where it does not

**Parsing, not rendering.**

```[text]
model  →  structured fields  →  deterministic title
          (opponent, venue,     (title_template)
           type, home/away)
       reviewed once, frozen as config
```

`normalize/summary.py` states the rule this preserves: *"Deterministic by design.
No model is involved, so the same feed always renders the same title, and a title
change is always traceable to a config change."*

A model in the render path breaks that twice over. The same event could get a
different title on different polls with nothing to explain why; and because
`content_hash` covers *feed* fields only, a title that changed for model reasons
would not register as a change at all — silent, untraceable drift.

The hard problem was never the naming convention. It is recovering
`opponent="Strikers", home=true` from whatever a coach typed. That is worth a
model, its output is reviewable structured data, and once reviewed it becomes an
alias or a venue row and the feed parses deterministically forever.

Because the title is a render and not data, getting naming wrong is cheap — every
event re-renders from stored fields without re-fetching. It is not a one-way door
and does not need a model to be right first time.

## 7. How the UI reaches the data

**Directly, via `repo.py` and `config.py` in the same process.** Configuration
does not go through the calsync API — see "Configuration is not in this API" in
[API.md](API.md) for the reasoning and, importantly, for what that does *not*
license: agents still propose rather than write, and still cannot approve.

Practically this means the onboarding flow is a thin web layer over functions
that already exist and are already tested — `config.apply`, `repo.set_staging`,
`sync_source(dry_run=True)` for the preview and the promotion gate.

It also means the poller and the UI are two writers on one SQLite file, so
`db.connect()` sets a busy timeout. Keep UI write transactions short.

## 8. State of play

All of it is built. Per-source staging (`sources.staging_collection`), the
diagnostics that feed the gate, `calsync stage` / `promote`, and the placement
check that makes promotion actually relocate events; then:

- **Feed inspection** — `inspection.inspect_feed`, a pure function from bytes to
  the derivations in §2. It creates nothing, which is the point: the paste-a-URL
  step has to show you what a feed contains *before* you decide whether to take
  it. Tested against all four recorded feeds, including the two that must
  correctly propose no team token at all.
- **The web console** — `calsync web`, in `web/`. Thin over `config.apply`,
  `repo.set_staging` and `sync_source(dry_run=True)`, exactly as §7 describes.
- **Clone-forward** — `repo.previous_season` / `onboarding.clone_forward`. A
  second season in the same sport carries the timezone, league, age group and
  alarm policy, and nothing else: the name, the feed and the aliases are what
  churn.

### What the console does with §5

The four gate conditions are the primary surface, rendered as four discrete
blocks rather than a progress bar — they do not happen in order, and three can
pass for weeks while the fourth waits on a coach.

Each unmet condition is a question with its own answer form: an unmatched
fixture offers the names it saw, ranked by frequency, as one-click activity
aliases; an unresolved venue offers a new venue row or an alias onto an existing
one. `unknown_types` is the exception and says so — the vocabulary lives in the
adapter, so there is nothing to fix from a browser.

"No games yet" is styled as *waiting*, never as an error, because it is the
expected state of a healthy feed in March.

### What is deliberately not there

Kids and sports are editable on one `/household` page, because a sport's emoji
reaches every title and a misspelled name is otherwise unfixable. `/venues` gets
a screen of its own for the reason in §3 — venues outlast teams, so the alias
table is the one piece of configuration whose value compounds. It carries the
merge, because three coaches typing three names for one park is how that table
actually goes wrong.

Settings and activity fields are still edited in SQLite, and there is no
team-deletion button — `children`
cascades through `activities` and `sources` to `event_state`, so deleting a kid
with a live team discards the record of every event already written and strands
those events in the family's calendar. Both delete paths check first and refuse
with the reason.
