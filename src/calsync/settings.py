"""Typed access to the settings table.

Settings are instance configuration, not per-family data: a deployment that
splits calendars by child rather than by event type changes one row here
instead of editing code.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    radicale_url: str
    radicale_user: str
    radicale_secret_ref: str
    collection_template: str
    collection_game_label: str
    collection_practice_label: str
    title_template: str
    multi_kid_style: str
    all_kids_label: str
    all_kids_threshold: int
    home_marker: str
    away_marker: str
    max_disappearance_pct: float
    max_disappearance_count: int
    sync_window_back_days: int
    sync_window_forward_days: int
    default_tz: str

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Settings":
        raw = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        missing = set(cls.__dataclass_fields__) - set(raw)
        if missing:
            raise KeyError(f"settings table missing keys: {sorted(missing)}; run migrate()")
        return cls(
            radicale_url=raw["radicale_url"],
            radicale_user=raw["radicale_user"],
            radicale_secret_ref=raw["radicale_secret_ref"],
            collection_template=raw["collection_template"],
            collection_game_label=raw["collection_game_label"],
            collection_practice_label=raw["collection_practice_label"],
            title_template=raw["title_template"],
            multi_kid_style=raw["multi_kid_style"],
            all_kids_label=raw["all_kids_label"],
            all_kids_threshold=int(raw["all_kids_threshold"]),
            home_marker=raw["home_marker"],
            away_marker=raw["away_marker"],
            max_disappearance_pct=float(raw["max_disappearance_pct"]),
            max_disappearance_count=int(raw["max_disappearance_count"]),
            sync_window_back_days=int(raw["sync_window_back_days"]),
            sync_window_forward_days=int(raw["sync_window_forward_days"]),
            default_tz=raw["default_tz"],
        )


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value)),
    )
    conn.commit()
