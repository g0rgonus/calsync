"""Import a YAML config into the database.

YAML is an import/export format, never the runtime source of truth — the schema
comment says so and the web UI will be CRUD over the same tables. This exists so
a deployment can be described in one reviewable file (docs/config.example.yaml)
instead of hand-written INSERTs.

Unrecognised keys under a source are preserved into ``sources.config`` rather
than dropped: several of them (``mapping``, ``venue_parse``) describe behaviour
that currently lives in adapter code, and silently discarding them would make
the file look honoured when it isn't.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

#: Keys the schema has real columns for. Everything else on a source is kept as
#: adapter config.
_SOURCE_COLUMNS = {
    "id", "activity", "kind", "shape", "tier", "trust_rank",
    "poll_interval", "url_template", "secret_ref", "enabled",
}
_DURATION = re.compile(r"^\s*(?P<qty>\d+)\s*(?P<unit>[smhd])?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ConfigError(RuntimeError):
    """The config could not be applied. Nothing is written when this is raised."""


def parse_duration(value, *, default_unit: str = "s") -> int:
    """``20m`` -> 1200. A bare number is taken in ``default_unit``."""
    if isinstance(value, (int, float)):
        return int(value) * _UNIT_SECONDS[default_unit]
    match = _DURATION.match(str(value))
    if match is None:
        raise ConfigError(f"could not read a duration from {value!r}")
    unit = (match.group("unit") or default_unit).lower()
    return int(match.group("qty")) * _UNIT_SECONDS[unit]


def _require(value, where: str):
    """Reject the TODO placeholders the example file is full of.

    Importing "TODO" as a real name is worse than refusing: it reaches the
    family calendar looking like data.
    """
    if value is None:
        raise ConfigError(f"{where} is required")
    if isinstance(value, str) and value.strip().upper() == "TODO":
        raise ConfigError(f"{where} is still 'TODO' — fill it in before importing")
    return value


def _venue_id(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM venues WHERE canonical_name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute("INSERT INTO venues (canonical_name) VALUES (?)", (name,))
    venue_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT OR IGNORE INTO venue_aliases (venue_id, alias, source) VALUES (?, ?, 'config')",
        (venue_id, name),
    )
    return venue_id


def load(path: str | Path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def apply(conn, data: dict) -> list[str]:
    """Upsert children, activities and sources. Returns a change log.

    One transaction: a config that fails halfway leaves nothing behind.
    """
    log: list[str] = []

    # Venues first: activities reference them by name.
    #
    # A known venue resolves with no model call, no latency and no variance —
    # which is why the alias list matters more than any clever parsing. Seed it
    # with the shorthands the coaches actually type.
    for venue in data.get("venues") or []:
        name = _require(venue.get("name"), "venue name")
        conn.execute(
            """
            INSERT INTO venues (canonical_name, short_name, address, lat, lon, pin_confirmed, geocoder)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_name) DO UPDATE SET
                short_name=excluded.short_name, address=excluded.address,
                lat=excluded.lat, lon=excluded.lon,
                pin_confirmed=excluded.pin_confirmed, geocoder=excluded.geocoder
            """,
            (
                name,
                venue.get("short_name"),
                venue.get("address"),
                venue.get("lat"),
                venue.get("lon"),
                int(bool(venue.get("pin_confirmed", False))),
                venue.get("geocoder", "config"),
            ),
        )
        venue_id = _venue_id(conn, name)
        # The canonical name is itself an alias, plus every shorthand seen in a
        # feed. Matching is COLLATE NOCASE, so case variants need no row.
        for alias in [name, *(venue.get("aliases") or [])]:
            if str(alias).strip().upper() == "TODO":
                continue
            conn.execute(
                "INSERT OR IGNORE INTO venue_aliases (venue_id, alias, source) "
                "VALUES (?, ?, 'config')",
                (venue_id, str(alias).strip()),
            )
        log.append(f"venue {name} ({len(venue.get('aliases') or []) + 1} aliases)")

    # Sports before children, because an activity references one by id.
    #
    # `db.migrate` seeds a catalog with INSERT OR IGNORE, which is what keeps an
    # edited emoji from being clobbered on the next upgrade — but it also means
    # the catalog is only ever *additive* from code. A deployment that needs
    # fencing has to be able to say so here, and to export it again.
    for sport in data.get("sports") or []:
        sid = _require(sport.get("id"), "sport id")
        conn.execute(
            """
            INSERT INTO sports (id, name, emoji, builtin)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, emoji=excluded.emoji
            """,
            (
                sid,
                _require(sport.get("name"), f"sport {sid} name"),
                _require(sport.get("emoji"), f"sport {sid} emoji"),
            ),
        )
        log.append(f"sport {sid}")

    for child in data.get("children") or []:
        cid = _require(child.get("id"), "child id")
        conn.execute(
            """
            INSERT INTO children (id, name, initial, birth_order, color, nicknames)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, initial=excluded.initial,
                birth_order=excluded.birth_order, color=excluded.color,
                nicknames=excluded.nicknames
            """,
            (
                cid,
                _require(child.get("name"), f"child {cid} name"),
                _require(child.get("initial"), f"child {cid} initial"),
                int(_require(child.get("birth_order"), f"child {cid} birth_order")),
                child.get("color"),
                json.dumps([n for n in (child.get("nicknames") or [])
                            if str(n).strip().upper() != "TODO"]),
            ),
        )
        log.append(f"child {cid}")

    for activity in data.get("activities") or []:
        aid = _require(activity.get("id"), "activity id")
        alarms = activity.get("alarm_policy") or {}
        home_venue = activity.get("home_venue") or (activity.get("mapping") or {}).get("home_venue")
        season = activity.get("season") or {}

        conn.execute(
            """
            INSERT INTO activities
                (id, child_id, name, sport_id, emoji, official_name, short_name,
                 league, age_group, home_venue_id, tz, season_start, season_end,
                 alarm_game_min, alarm_practice_min, enabled)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                child_id=excluded.child_id, name=excluded.name,
                sport_id=excluded.sport_id, emoji=excluded.emoji,
                official_name=excluded.official_name, short_name=excluded.short_name,
                league=excluded.league, age_group=excluded.age_group,
                home_venue_id=excluded.home_venue_id, tz=excluded.tz,
                season_start=excluded.season_start, season_end=excluded.season_end,
                alarm_game_min=excluded.alarm_game_min,
                alarm_practice_min=excluded.alarm_practice_min,
                enabled=excluded.enabled
            """,
            (
                aid,
                _require(activity.get("child"), f"activity {aid} child"),
                _require(activity.get("name"), f"activity {aid} name"),
                _require(activity.get("sport"), f"activity {aid} sport"),
                activity.get("emoji"),
                activity.get("official_name"),
                activity.get("short_name"),
                activity.get("league"),
                activity.get("age_group"),
                _venue_id(conn, home_venue) if home_venue else None,
                _require(activity.get("tz"), f"activity {aid} tz"),
                str(season.get("start")) if season.get("start") else None,
                str(season.get("end")) if season.get("end") else None,
                parse_duration(alarms.get("game", "90m"), default_unit="m") // 60,
                parse_duration(alarms.get("practice", "30m"), default_unit="m") // 60,
                int(activity.get("enabled", 1)),
            ),
        )
        for alias in activity.get("aliases") or []:
            if str(alias).strip().upper() == "TODO":
                continue
            conn.execute(
                "INSERT OR IGNORE INTO activity_aliases (activity_id, alias, source) "
                "VALUES (?, ?, 'config')",
                (aid, str(alias)),
            )
        log.append(f"activity {aid}")

    for source in data.get("sources") or []:
        sid = _require(source.get("id"), "source id")
        extra = {k: v for k, v in source.items() if k not in _SOURCE_COLUMNS}
        conn.execute(
            """
            INSERT INTO sources
                (id, activity_id, kind, shape, tier, trust_rank, poll_interval_s,
                 url_template, secret_ref, config, enabled)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                activity_id=excluded.activity_id, kind=excluded.kind,
                shape=excluded.shape, tier=excluded.tier,
                trust_rank=excluded.trust_rank,
                poll_interval_s=excluded.poll_interval_s,
                url_template=excluded.url_template, secret_ref=excluded.secret_ref,
                config=excluded.config, enabled=excluded.enabled
            """,
            (
                sid,
                _require(source.get("activity"), f"source {sid} activity"),
                _require(source.get("kind"), f"source {sid} kind"),
                source.get("shape", "feed"),
                int(source.get("tier", 2)),
                int(source.get("trust_rank", 2)),
                parse_duration(source.get("poll_interval", "20m"), default_unit="m"),
                source.get("url_template"),
                source.get("secret_ref"),
                json.dumps(extra),
                int(source.get("enabled", 1)),
            ),
        )
        if extra:
            log.append(f"source {sid} (kept {', '.join(sorted(extra))} as adapter config)")
        else:
            log.append(f"source {sid}")

    for key, value in (data.get("settings") or {}).items():
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (str(key), str(value)),
        )
        log.append(f"setting {key}")

    conn.commit()
    return log
