# Fixtures

`games.ics` is **generated, never edited by hand — and so is its input**:

```bash
.venv/bin/python mac/Tests/CalsyncMirrorCoreTests/Fixtures/generate.py
```

The generator builds `Event`s and puts them through `render()`, which is the
call `sync.py` makes. That matters more than it looks: an earlier version ran
the real serializer over a `RenderedEvent` it constructed itself, with a zoned
`starts_at` no adapter produces. The output was genuinely `to_ics`'s, and it was
still a format Radicale has never served — `DTSTART;TZID=...` rather than
`DTSTART:...Z` — so the Swift parser was certified against a shape that does not
exist, and every mirrored event went into EventKit with no timezone. Generating
the output is not enough; the input has to come from the pipeline too.

Every name, team, venue and address in it is invented, and that is a rule rather
than an accident — see the repo README's "Fixtures are invented, always". The
shapes are the tests: the fold that lands mid-word, `-PT1H30M` for a 90-minute
alarm, the exclusive `DTEND` on a DATE. If a Swift test fails after you
regenerate this, read `Sources/CalsyncMirrorCore/ICS.swift` before changing the
assertion — calsync's serialization probably moved, and the phones would have
noticed before the test did.
