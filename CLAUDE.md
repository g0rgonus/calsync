# CLAUDE.md

calsync turns the team schedules coaches publish into one family calendar on a
CalDAV server. It only ever changes events it created, and when a feed looks
wrong, or it can't tell what an event is, it holds and asks a person rather than
guessing.

[CONTRIBUTING.md](CONTRIBUTING.md) is the rulebook: setup, layout, the
invariants, the invented world for fixtures, and how releases work. Read it
before changing anything. What calsync writes is in [docs/events.md](docs/events.md);
each connector has a doc in `docs/connectors/`.

## Working on it

```bash
.venv/bin/pytest                                          # keep green
scripts/dev-stack.sh && CALSYNC_ACCEPTANCE=1 .venv/bin/pytest tests/test_acceptance.py
cd mac && swift test                                      # Mac mirror
```

## Rules that are easy to break

- **The invariants in CONTRIBUTING are contracts.** Never loosen a guard or a
  threshold to make a test pass.
- **No real names, places or feed payloads, anywhere.** Use the invented world
  in CONTRIBUTING and keep the shapes of what you rewrite.
- **The title is a render.** Never store or parse one.
- **Version in one place:** `__version__` in `src/calsync/__init__.py`, bumped
  with a `CHANGELOG.md` section in the same pull request.
- **Comments say why in a line or two.** No history, no accounts of past bugs.
