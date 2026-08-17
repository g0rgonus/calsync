# Requirement: Radicale deployment

A work order for the infrastructure side. calsync treats this as an opaque
CalDAV endpoint — it is not coupled to Radicale specifically ([PLAN.md
§5b](../PLAN.md)) — so anything meeting the contract in §1 will do.

**Config snippets below are illustrative.** Radicale's config format and the
rights permission letters changed between 2.x and 3.x, and this was written
without a server to test against. Treat §1 and §6 as the specification and
verify the syntax against the Radicale docs for the version you install.

---

## 1. The contract

What calsync's CalDAV target actually depends on. Everything else is
deployment preference.

| # | Requirement | Why |
|---|---|---|
| R1 | Reachable at a stable base URL from the box running calsync | Configured once in the `targets` row |
| R2 | **ETags on `PUT` responses**, and honours `If-Match` / `If-None-Match` | Conflict detection. Without it, a concurrent edit is silently overwritten |
| R3 | Returns **412** on a stale `If-Match` | The writer raises rather than clobbering |
| R4 | **Preserves unknown `X-` properties** verbatim on round-trip | `X-CALSYNC-UID`, `-SOURCE`, `-HASH` carry provenance |
| R5 | Preserves `LOCATION` text verbatim | Venue name plus street address is the whole of what gets somebody to the right car park |
| R6 | Accepts resource names containing mixed case and hyphens (`360Player-event-4823901.ics`) | UIDs come from upstream and are not ours to rewrite |
| R7 | `MKCALENDAR`, or pre-created collections | The target calls `ensure_collection`; 405/409 are treated as "already exists" |
| R8 | Two principals: one read-write, one read-only | §3 |

**R5 was originally about `X-APPLE-STRUCTURED-LOCATION`, and this server fails
that.** Radicale re-serializes the property and reads the comma in
`geo:lat,lon` as a value separator, keeping only the latitude — a pin at
longitude 0, about 6000km out. calsync no longer emits an exact pin at all
(a name and an address resolve fine in a maps app), so the requirement is now
the weaker and sufficient one above. `tests/test_acceptance.py` checks it.

R4 is the one worth actually testing rather than assuming — a
database-backed server that normalizes properties can quietly drop both.

---

## 2. Collections

Two, matching the default `collection_template = "{type}"`:

```
<base>/calsync/games/
<base>/calsync/practices/
```

Collection names come from calsync config, so if the template changes to
`{child}` the collections become `james/`, `patrick/`, `millie/`. **Don't
hard-code the two names anywhere in the infrastructure** — let calsync create
them, or be ready to add more.

Display names and colours are cosmetic here; the consuming tool decides what
they map to downstream.

---

## 3. Principals and rights

| User | Access | Used by |
|---|---|---|
| `calsync` | read-write on `calsync/*` | the poller — the only writer |
| `calreader` | read-only on `calsync/*` | the Mac sync tool, any device subscriptions |

Separate credentials so a bug in the read side cannot corrupt the store, and
so a device subscription can be revoked without rotating the writer's
password.

Passwords hashed with bcrypt in an htpasswd file — not plaintext, even on a
private network.

```ini
# rights — ILLUSTRATIVE, verify permission letters against current docs
[calsync-write]
user: calsync
collection: calsync(/.*)?
permissions: RW

[calsync-read]
user: calreader
collection: calsync(/.*)?
permissions: R
```

---

## 4. Storage and history

Use the file-backed storage (`multifilesystem`) rather than anything
database-backed. One `.ics` per event in a directory tree is greppable,
trivially backed up, and satisfies R4/R5 by construction.

**Git history is worth having.** Radicale can run a hook after storage
changes; pointing it at `git commit` gives free, human-readable version
history of every schedule change — which pairs with how much of this design is
about provenance and reversibility.

Two caveats:

- The hook fires **per storage operation**. A first-run backfill of a full
  season could produce a commit per event. Either accept the noise or import
  in a quiet window — don't be alarmed by a hundred commits appearing at once.
- The git repo grows forever. Fine at this scale (a few hundred small text
  files a year), but it is not self-pruning.

Back up the storage directory independently of git. The repo protects against
bad edits; it does not protect against losing the disk.

---

## 5. Network exposure

**Tailscale only. No public ingress.** This holds children's names, schedules,
and physical locations with timestamps — it is not something to put behind
basic auth on the open internet.

Plain HTTP on the tailnet is acceptable since Tailscale encrypts transport. If
you'd rather have TLS end-to-end, `tailscale serve` will terminate it without
a certificate dance.

Resource sizing is a non-issue: a few hundred events a year, one writer, a
20-minute poll interval. Anything that runs Python will run this.

---

## 6. Acceptance checks

Run these before pointing calsync at it. They test the contract, not the
install — each maps to a requirement above.

```bash
BASE=http://radicale.<tailnet>.ts.net/calsync
AUTH=calsync:<password>

# R7 — create a collection (201, or 405/409 if it already exists)
curl -su "$AUTH" -X MKCALENDAR "$BASE/games/" -o /dev/null -w '%{http_code}\n'

# R2/R6 — create an event with an upstream-style UID; expect 201 and an ETag
curl -su "$AUTH" -X PUT "$BASE/games/360Player-event-4823901.ics" \
  -H 'Content-Type: text/calendar; charset=utf-8' \
  -H 'If-None-Match: *' \
  --data-binary @sample.ics -D - -o /dev/null | grep -i -E '^(HTTP|etag)'

# R4/R5 — read it back and confirm nothing was normalized away
curl -su "$AUTH" "$BASE/games/360Player-event-4823901.ics" \
  | tr -d '\r' | sed ':a;N;$!ba;s/\n //g' \
  | grep -E 'X-CALSYNC|LOCATION'

# R3 — stale If-Match must be refused, not applied
curl -su "$AUTH" -X PUT "$BASE/games/360Player-event-4823901.ics" \
  -H 'Content-Type: text/calendar' -H 'If-Match: "definitely-stale"' \
  --data-binary @sample.ics -o /dev/null -w '%{http_code}\n'

# R8 — the read-only principal must be refused a write
curl -su "calreader:<password>" -X PUT "$BASE/games/probe.ics" \
  -H 'Content-Type: text/calendar' --data-binary @sample.ics \
  -o /dev/null -w '%{http_code}\n'
```

Pass criteria:

| Check | Expected |
|---|---|
| MKCALENDAR | `201`, or `405`/`409` on re-run |
| PUT new | `201` **with an `ETag` header** |
| Read back | `X-CALSYNC-*` present, and `LOCATION` carrying the venue name and street address verbatim |
| Stale `If-Match` | `412` |
| Read-only write | `403` |

The `sed` in the read-back step unfolds RFC 5545 line wrapping — the
structured-location property is long enough to be split across lines, and
grepping the raw output will miss it.

Generate `sample.ics` from the codebase so the check exercises the real
serializer:

```bash
python -c "
import sys; sys.path.insert(0,'src')
from tests.test_targets import *
from calsync.render import render
from calsync.db import open_db
from calsync.settings import Settings
import tempfile, pathlib
s = Settings.load(open_db(pathlib.Path(tempfile.mkdtemp())/'d.db'))
" > /dev/null   # see tests/test_targets.py for a ready-made rendered event
```

---

## 7. What will bite

- **A server that normalizes `X-` properties** silently breaks provenance and
  the exact pin. That is why R4/R5 are acceptance checks rather than
  assumptions.
- **Missing ETags** turn every write into a blind overwrite. If the server
  doesn't return them, the conflict detection in the CalDAV target is inert.
- **The git hook on bulk import** — see §4.
- **Rights syntax** is the most likely thing to be wrong on first attempt, and
  it fails open or closed in confusing ways. The §6 read-only check catches
  the dangerous direction.
- **Radicale 2 vs 3 config** differ enough that a copied snippet from an old
  blog post will not work.

---

## 8. What calsync needs back

Once it's up, three values for the `targets` row:

1. Base URL (e.g. `http://radicale.<tailnet>.ts.net/calsync`)
2. Writer username
3. Writer password → stored in the secret store, referenced by `secret_ref`,
   **never** written into the database or a config file
