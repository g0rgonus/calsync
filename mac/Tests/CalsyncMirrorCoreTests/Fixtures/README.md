# Fixtures

`games.ics` is **generated, never edited by hand**:

```bash
.venv/bin/python mac/Tests/CalsyncMirrorCoreTests/Fixtures/generate.py
```

Every name, team, venue and address in it is invented, and that is a rule rather
than an accident — see the repo README's "Fixtures are invented, always". The
shapes are the tests: the fold that lands mid-word, `-PT1H30M` for a 90-minute
alarm, the exclusive `DTEND` on a DATE. If a Swift test fails after you
regenerate this, read `Sources/CalsyncMirrorCore/ICS.swift` before changing the
assertion — calsync's serialization probably moved, and the phones would have
noticed before the test did.
