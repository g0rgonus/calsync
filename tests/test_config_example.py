"""docs/config.example.yaml imports as written, so the example can't drift from config.apply."""

from __future__ import annotations

from pathlib import Path

from calsync import config, db, repo

EXAMPLE = Path(__file__).resolve().parent.parent / "docs" / "config.example.yaml"


def test_the_example_imports_and_every_source_loads(tmp_path):
    conn = db.open_db(tmp_path / "calsync.db")
    config.apply(conn, config.load(EXAMPLE))
    sources = {s.id: s for s in repo.list_sources(conn)}
    assert set(sources) == {"p360-luke-jedi", "tr-rey-rogues"}
    # The stored URL carries a placeholder, never a token.
    assert all("{{secret:" in s.url_template for s in sources.values())
