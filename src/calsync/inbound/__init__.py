"""Inbound sources: a team whose schedule is pushed to calsync rather than fetched.

A source of kind ``inbound`` is onboarded like any other — one per team, per
kid, staged until promoted — but instead of a feed URL it gets an API key, and a
tool holding that key sends events one at a time over a dedicated port
(`app.py`). Nothing else about the pipeline changes, and that is the design:
what a tool sends is stored (`repo.inbound_items`), and the sync loop reads the
store where it would otherwise have fetched (`sources/inbound.py`). The title
template, routing, staging, warm-ups, venue aliases, alarms and the guard all
apply exactly as they do to a feed, because it is the same `sync_source`.

Three properties a reader is likely to want to change, and should not:

- **The sender describes an event; it never renders one.** No title, no
  calendar, no child. The source fixes whose team it is; the title is composed
  by `normalize/title.py`; the collection is `routing.collection_for`'s call. A
  field calsync would ignore is refused, so a sender learns that rather than
  wondering why its title never appears.
- **Nothing disappears by omission.** Every change is per event, and a
  cancellation is an explicit ``DELETE`` — the inbound equivalent of a feed's
  ``STATUS:CANCELLED``, honoured the same way. Items are never removed from the
  store, so an absence can only mean the store itself is damaged, which the
  adapter refuses to read as "everything cancelled".
- **A series is expanded here, not written as an RRULE** (`recurrence.py`).
"""
