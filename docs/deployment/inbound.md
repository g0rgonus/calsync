# The inbound API

A team with no feed — a PDF from the coach, an email, a whiteboard photo — can
still be a calsync source. Onboard it in the console as **"a tool sends the
schedule"**, hand the tool the source id and key it shows you, and the tool
sends events one at a time. They go through the same sync loop as a feed:
staged first, titled by your template, routed by your rules, guarded the same
way.

## Turning it on

```bash
# .env
CALSYNC_INBOUND_REPLICAS=1
# optional
CALSYNC_INBOUND_PORT=8732
CALSYNC_INBOUND_BIND=0.0.0.0     # or the one host address the proxy reaches
```

`docker compose up -d` then publishes port 8732 on the host. It is **not**
behind `proxy` and does not share the console's port: nothing on 8732 but the
routes below, so a firewall rule admitting 8732 admits nothing else. Off by
default so an upgrade never opens a port.

## Documentation on the service itself

No key needed, generated from one table in `src/calsync/inbound/contract.py`
and tested against the routes, so none of them can drift from the API:

| | |
|---|---|
| `/inbound/v1/docs` | A page for a person: every call with a ready-to-paste curl, the fields, the safeguards, a season worked through. |
| `/inbound/v1/openapi.json` | OpenAPI 3.1, for an assistant platform that imports a spec as tools. Its `servers` entry is whatever address it was fetched through, so fetch it through Pangolin. |
| `/inbound/v1` | The same, as compact JSON. |

## Auth

| Header | |
|---|---|
| `X-Calsync-Key` | The source's key. One key opens one team. |
| `Authorization` | **Never read.** Left to whatever proxy is in front. |

The key is 256 random bits, shown once in the console; calsync stores only its
SHA-256. Lost keys are replaced (**Replace the API key** on the team's page),
never recovered, and the old one stops working at once.

### The household read key

`GET /inbound/v1/calendar` reads **every team's events, for every kid**. It returns
exactly the read API's `GET /v1/events` body, built by the same function, with the
same bounded window and the `child` / `activity` filters. It takes its own key,
sent in the same `X-Calsync-Key` header:

- made, replaced or removed on the console's **settings** page, under "Household
  read key", and shown once there, with only its hash stored;
- **read-only**: it cannot write, and it opens no team's endpoints;
- **a team key is refused here**, because each team key opens one team, and the
  whole family's schedule is a wider grant;
- "last used" on the settings page counts its reads.

It holds only what calsync wrote. Appointments added by hand in a calendar app
are not in calsync, so they are not here either.

### Admin keys

The `/inbound/v1/admin` calls create and manage teams. They take an **admin
key**, one per tool, made and revoked on the console's settings page under
"Admin keys"; team keys and the read key are refused. A new team starts staged,
and its key is in the create response only. Every create needs an
`Idempotency-Key` header: a retry with the same value returns the team with
`key_already_issued` instead of a second team, for a day. A lost team key is
replaced with `POST …/admin/teams/{source}/key`; so is the key of a create whose
retry got `409 already_exists` for that same team. Kids are added in the console.

A staged team is reviewed with `…/review`: the console's four checks, every
event with the calendar it will land in, and timed clashes with other teams.
`…/live` makes it live behind the same gate as the console, and syncs so the
events move at once; only the console can override a failed check.
`DELETE …/{source}` discards a staged team, taking all its events off the
staging calendar; a live team is refused and is retired in the console.

`…/admin/events/status` marks one event `not_attending` or `cancelled`, or puts
it back with `null`, for any team's event. It is the console's mark: it survives
later syncs, hiding an event that has started is refused (putting one back never
is), and a warm-up is changed through its game. Every hide sends a Pushover
alert naming the key, the team and the time, when Pushover is set up. An admin key also reads `GET /inbound/v1/calendar`, which is
where a tool finds the event's `uid`.

A 401 carries `WWW-Authenticate: CalsyncKey`, not `Basic`, so a client in
"extended compatibility" mode does not answer it by resending the proxy's
password in a loop. An unknown source and a wrong key are the same 401.

## Pangolin

The resource for this service:

- **Target**: the calsync host, port 8732 (`CALSYNC_INBOUND_PORT`), HTTP.
- **Basic Header Auth**: on, with its own user and a long random password
  (`openssl rand -base64 24`). Extended compatibility on. This is Pangolin's
  credential, not calsync's — they are deliberately separate.
- **No SSO-only rule on `/inbound/*`.** A tool cannot complete a login page;
  the Basic header has to be enough to reach the target.
- Not the console's resource. Do not route `/inbound` through the resource that
  fronts port 8730: that one has no login behind Pangolin's.

The tool then sends both credentials, each in its own header:

```bash
curl -u 'calsync-bot:PANGOLIN_PASSWORD' \
     -H 'X-Calsync-Key: csk_…' \
     https://inbound.example.org/inbound/v1/sources/in-herons
```

A JSON body naming the team is success. HTML (a login page) means the Pangolin
rule is still SSO-gated; a JSON 401 with `bad_credential` means Pangolin let
you through and the calsync key is wrong; a Pangolin 401 means its own
credential is wrong. Custom `X-` headers pass through Pangolin's proxy
unchanged, which is what this design relies on — confirm with the call above
after any Pangolin upgrade.

## Events

```
PUT    /inbound/v1/sources/{source}/events/{id}                      201 created, 200 replaced/unchanged
GET    /inbound/v1/sources/{source}/events/{id}                      with occurrences and overrides
GET    /inbound/v1/sources/{source}/events                           everything sent, cancelled included
DELETE /inbound/v1/sources/{source}/events/{id}                      204; 404 if never sent
PUT    /inbound/v1/sources/{source}/events/{id}/occurrences/{date}   change one date of a series
DELETE /inbound/v1/sources/{source}/events/{id}/occurrences/{date}   cancel one date; 204
GET    /inbound/v1/sources/{source}                                  which team this key writes to
GET    /inbound/v1/calendar?from=&to=&child=&activity=               every team's events (household read key)
GET    /inbound/v1                                                   this list, no key needed

GET    /inbound/v1/admin/teams                                       every team (admin key)
POST   /inbound/v1/admin/teams                                       create a team; Idempotency-Key required
GET    /inbound/v1/admin/teams/{source}/review                       checks, events, clashes
POST   /inbound/v1/admin/teams/{source}/live                         make a staged team live
DELETE /inbound/v1/admin/teams/{source}                              discard a staged team
POST   /inbound/v1/admin/events/status                               not attending / cancelled / put back
POST   /inbound/v1/admin/teams/{source}/pause                        stop writing its events
POST   /inbound/v1/admin/teams/{source}/resume                       start again
POST   /inbound/v1/admin/teams/{source}/key                          new team key; the old one stops
DELETE /inbound/v1/admin/teams/{source}/key                          revoke the team key
```

`{id}` is the tool's own: 1–128 of `A-Z a-z 0-9 . _ -`. Repeating a PUT is
free — same ETag, no calendar write. That is why the tool picks the id rather
than calsync issuing one: a create that times out can be sent again without
making a second event.

### List, match, then write

A tool is not expected to remember its ids. To change something, it lists
first, finds the event, and writes to the id it found:

1. `GET …/events?from=2026-10-27&to=2026-10-27` — only events with a date in
   that window, carrying only those dates. Each has its `external_id`, its
   fields and its `etag`.
2. Pick the one that matches.
3. `PUT …/events/{external_id}` to change it, or
   `…/events/{external_id}/occurrences/2026-10-27` for that one date.

Three safeguards for a tool that skips step 1:

| | |
|---|---|
| **Duplicate check** | A PUT to an id calsync has never seen, overlapping a live event of the same type on the same day, is **409 `possible_duplicate`**, naming the existing id and dates. Nothing is written. Update that id instead — or, if both events are real (two practices that day), resend with `?allow_overlap=true`. |
| `If-None-Match: *` | Create-only: **412 `already_exists`** if the id has been used, rather than replacing that event. |
| `If-Match: "<etag>"` | Conditional: **412 `etag_mismatch`** if the event changed since it was read. |

| Field | |
|---|---|
| `type` | `practice` or `game`. Required. |
| `start`, `end` | Local wall-clock ISO 8601 (`2026-10-20T17:45:00`) in `tz`. An offset is accepted and converted. A bare date is all-day; its `end` is the last day. |
| `tz` | IANA zone. Defaults to the team's. |
| `label` | The short word in the title — "Swim", "Dryland". |
| `location` | Free text, resolved through the venue table like any feed's. |
| `opponent`, `home` | Games only. Leave `home` out when unknown. |
| `notes`, `url` | Into the event body. |
| `rrule` | `FREQ=DAILY\|WEEKLY`, `INTERVAL`, `BYDAY`, `WKST=MO`, and exactly one of `UNTIL` or `COUNT`. calsync expands it into one event per date; there is no default season end. |
| `exdates` | Dates the series skips. |
| `part_of` | The `external_id` of the event this belongs to — see [Meets](#meets). |

Refused, by name, with the reason: `title`, `calendar`, `child`, `team`,
`status`, `uid`. calsync composes the title and picks the calendar; one source
is one team for one child; cancelling is a `DELETE`.

Errors are JSON: `{"error": {"code", "detail", "errors": [{"field", "code", "detail"}]}}`.

### Meets

A meet arrives in two stages, and `part_of` joins them:

1. **Announced:** one all-day event across the meet's days, `type: "game"`, the meet's name as `label`, no opponent.
2. **Session schedule out:** each session the swimmer is in is a `game` starting at the official session time, with events and estimated times in `notes`. Its warm-up is a `practice`. Both name the meet in `part_of`.

While any live part points at the meet, calsync takes the all-day event off the calendar. It comes back if every part is cancelled, because the meet is still on even when the session list is withdrawn. The sender never deletes it. Parts never count as duplicates of their own meet; two parts at the same time still do.

- **The whole meet goes, not just the days that have parts.** The placeholder is one multi-day event. A three-day meet with sessions only on Saturday shows Saturday's sessions and nothing on Friday or Sunday. That's right when the sender sends only the sessions the swimmer is in, so **send a swimmer's parts once their whole schedule for the meet is known**, not day by day as it trickles out. A lone warm-up hides the meet the same way.
- **Responses say what is on the calendar.** A replaced meet reports `"cancelled": false`, because nobody cancelled it, along with `"superseded_by": [its live parts]` and `"on_calendar": false`. Read `on_calendar` to know whether an event is showing.
- **A replaced meet is not a duplicate candidate.** It is on no calendar; its parts are, and they still count.
- **Naming a cancelled meet** in `part_of` is accepted, with a `warnings` entry in the response, since it can also mean a cancellation was missed.

One level only:
- the meet must already have been sent;
- a part cannot have parts of its own;
- an event with parts cannot become a part.

Each of these is a 422 naming the problem.

A team that receives warm-ups this way should keep its own warm-up setting at 0. Otherwise calsync also generates one in front of every session.

### Examples

```bash
H=(-u 'calsync-bot:PANGOLIN_PASSWORD' -H 'X-Calsync-Key: csk_…' -H 'Content-Type: application/json')
U=https://inbound.example.org/inbound/v1/sources/in-herons

# A weekly practice for the season
curl "${H[@]}" -X PUT "$U/events/tue-swim" -d '{
  "type": "practice", "label": "Gold group swim",
  "start": "2026-10-20T17:45:00", "end": "2026-10-20T19:30:00",
  "location": "Wrenfield Aquatic Center",
  "rrule": "FREQ=WEEKLY;BYDAY=TU;UNTIL=20270531T235959Z",
  "notes": "Arrive 10–15 min early"}'

# One week moves to 18:30 at a different pool
curl "${H[@]}" -X PUT "$U/events/tue-swim/occurrences/2026-10-27" \
  -d '{"start": "2026-10-27T18:30:00", "location": "Larkmoor Pool"}'

# Thanksgiving week is off
curl "${H[@]}" -X DELETE "$U/events/tue-swim/occurrences/2026-11-24"

# What was sent, with every date it produces
curl "${H[@]}" "$U/events/tue-swim"
```

## What it will and will not do

- **One key, one team.** A key cannot read or write another source, touch
  configuration, promote, approve anything, or reach the console.
- **Staged first.** A new inbound team writes to the onboarding calendar until
  you promote it, like a feed.
- **A cancellation is explicit and is obeyed.** `DELETE` is treated like a
  feed's `STATUS:CANCELLED`: applied without the disappearance guard, because
  it is not an absence. A series shortened or moved by a PUT cancels the dates
  it no longer produces the same way.
- **One writer per team at a time.** A write holds the team's lock and SQLite's
  write lock from its first read to its save, so two tools sending the same
  ETag cannot both succeed: one gets 412. 503 with `Retry-After` means the
  poller had the database; nothing was read or written.
- **Weak ETags match.** `If-Match: W/"…"` is compared as if strong, because a
  compressing proxy rewrites tags that way on the way out.
- **Throttled**: 60 writes a minute per team (429 with `Retry-After`).
- **The key is checked before the body is read**, and the console shows when
  each key was last used — reads included, since a leaked key used only to
  read the schedule leaves no other trace.
- **Logged**: source, event id, action and outcome, plus refused keys with the
  connecting address — and any `X-Forwarded-For`, shown as `claimed via peer`
  rather than trusted. Never the key, never a body.
- **Not TLS.** The service speaks plain HTTP; Pangolin terminates TLS. Keep the
  hop from Pangolin's connector to 8732 on a network you trust, and use
  `CALSYNC_INBOUND_BIND` and the firewall to admit only that connector.
