# Source adapter: flag football (app not yet identified)

Status: **partial**. Identity fields recovered from a legacy n8n sync table. No
full payload inspected. The publishing app has not been named yet; the n8n table
this came from was reused from the TeamReach sync, so the table name there is
misleading.

## Trap 1: the UID is not stable

Observed UIDs, two polls 19 minutes apart:

```[text]
poll @ 17:47   12782317  2026-04-24T17:47:04.859629
poll @ 18:06   12782317  2026-04-24T18:06:30.105264
               └─event─┘ └──── generation timestamp ────┘
```

Same event, different UID. The suffix is *generation* time, not event time — and
within one poll the values step by microseconds (`…105200`, `…105220`,
`…105236`), which is a loop stamping `now()` per event as it is emitted.

Only the leading id is identity. **Policy: `extract`, `^(?P<id>\d{8})`** — but
confirm the id is genuinely 8 digits across a wider sample before pinning the
width; a `-`-free concatenation gives no delimiter to key on, so this is the one
fragile part of the mapping.

### Why this is the dangerous failure

Unstable UIDs do not look like an error. Every poll classifies the whole season
as new and orphans the previous copy, so the calendar doubles silently. The
disappearance guard in `diff.py` does not catch it on its own — that guard
withholds *deletions*, and this failure is all *creations*.

The identity guard exists for exactly this: total turnover (nothing known
matched, nothing incoming recognised) holds both halves. See
`test_identity_break_holds_creations_too`.

## Trap 2: `SEQUENCE` is a flag, and it decrements

Observed: the same event carried `1` at 17:47 and `0` at 18:06.

RFC 5545 requires `SEQUENCE` to increase monotonically. This one goes backwards,
so it cannot be a revision counter — most likely a new/updated boolean, possibly
set by the n8n workflow rather than the publisher. Either way it is unusable for
change detection, and this is what makes the point general: across three sources
the same field means three different things.

| Source | `SEQUENCE` | Usable |
|---|---|---|
| TeamReach | **none published** — n8n derived one from `LAST-MODIFIED` | n/a |
| Player360 | unix mtime, bumped after `DTEND` | no — churns |
| this source | `1` → `0` | no — non-monotonic |

Three sources, and not one of them supplies a `SEQUENCE` that means what RFC 5545
says it means. `content_hash` over content fields is the only signal that
survives all three.

## Still unknown

Everything else: export format, auth, cancellation signal, category vocabulary,
location shape. Likely a `document` or `relay` shape rather than `feed` if the
app has no export at all.
