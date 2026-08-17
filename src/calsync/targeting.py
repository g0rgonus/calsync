"""Choose and build the configured calendar target.

`targets/__init__.py` advertises a registry — three kinds are registered — but
until now only two of them were reachable: the CLI hardcoded "ics_file when
--out is given, otherwise CalDAV", and the console grew its own copy of the
CalDAV construction. Pluggability nothing can select is not pluggability.

This is the one place that turns configuration into a target, so the CLI, the
poller and the console cannot drift apart about where events go.

It also fails *early*. A target that cannot possibly work should say so before
a feed is fetched and diffed, not raise from the middle of the write loop with
half a season applied — especially for Google, which is registered and payload-
complete but has no authenticated transport, so it would otherwise look
configurable right up until the moment it mattered.
"""

from __future__ import annotations

import json

from .secrets import SecretError, SecretStore
from .settings import Settings
from .targets import TargetError, build
from .targets.http import HttpTransport

#: What `target_kind` may be set to. `google` is deliberately included: it is a
#: real, tested payload builder, and pretending it does not exist would be as
#: misleading as pretending it works.
KINDS = ("caldav", "ics_file", "google")


def build_target(
    conn,
    *,
    kind: str | None = None,
    out_dir: str | None = None,
    secrets: SecretStore | None = None,
):
    """The target this deployment writes to.

    ``out_dir`` wins over everything: ``--out`` is the "show me what would
    happen, in files I can read and diff" escape hatch, and it must not depend
    on the configured target being reachable.
    """
    if out_dir:
        return build("ics_file", directory=out_dir)

    settings = Settings.load(conn)
    kind = kind or settings.target_kind
    if kind not in KINDS:
        raise TargetError(
            f"unknown target kind {kind!r}; target_kind must be one of "
            f"{', '.join(KINDS)}"
        )

    if kind == "ics_file":
        raise TargetError(
            "target_kind is 'ics_file' but no directory was given; pass --out DIR"
        )

    if kind == "google":
        return _google(conn, secrets)

    secrets = secrets or SecretStore()
    password = secrets.get(settings.radicale_secret_ref)
    return build(
        "caldav",
        base_url=settings.radicale_url,
        transport=HttpTransport(username=settings.radicale_user, password=password),
        username=settings.radicale_user,
        password=password,
    )


def _google(conn, secrets):
    """Refuse clearly rather than fail halfway through a write.

    The Google target's payload builder is complete and tested — event ids,
    extendedProperties, the lot. What does not exist is an authenticated
    transport: Google needs an OAuth token exchange, and there is no equivalent
    of `targets/http.py` for it. Building one that nobody can run against real
    Google would be untested network code in the exact seam that has already
    produced one silent bug this project knows about (the dropped ETag), so it
    is left undone on purpose rather than written blind.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'google_calendar_map'"
    ).fetchone()
    calendar_map = json.loads((row["value"] if row else "") or "{}")

    missing = []
    if not calendar_map:
        missing.append(
            "google_calendar_map (a JSON object mapping each collection name to "
            "a Google calendar id)"
        )
    missing.append(
        "an authenticated transport — the payload builder is complete and "
        "tested, but nothing implements Google's OAuth token exchange yet"
    )
    raise TargetError(
        "the Google target cannot be used yet. Missing: " + "; ".join(missing)
    )


def describe(conn, *, out_dir: str | None = None) -> str:
    """One line naming where events would go. For status output, not logic."""
    if out_dir:
        return f"ics files in {out_dir}"
    settings = Settings.load(conn)
    if settings.target_kind == "caldav":
        return f"CalDAV at {settings.radicale_url}"
    return settings.target_kind


__all__ = ["KINDS", "build_target", "describe", "TargetError", "SecretError"]
