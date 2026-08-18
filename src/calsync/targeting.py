"""Choose and build the configured calendar target.

`targets/__init__.py` advertises a registry, but for a long time nothing could
select from it: the CLI hardcoded "ics_file when --out is given, otherwise
CalDAV", and the console grew its own copy of the CalDAV construction.
Pluggability nothing can select is not pluggability.

Being in the registry and being offered as a destination are separate things —
see WITHDRAWN.

This is the one place that turns configuration into a target, so the CLI, the
poller and the console cannot drift apart about where events go.

It also fails *early*. A target that cannot possibly work should say so before
a feed is fetched and diffed, not raise from the middle of the write loop with
half a season applied.
"""

from __future__ import annotations

from .secrets import SecretError, SecretStore
from .settings import Settings
from .targets import TargetError, build
from .targets.http import HttpTransport

#: What `target_kind` may be set to, and what `--target` accepts.
KINDS = ("caldav", "ics_file")

#: Registered in `targets/` but not offered as a destination, with the reason.
#:
#: `targets/google.py` is a complete, tested payload builder — event ids,
#: extendedProperties, calendar-to-calendar moves. What is missing is the OAuth
#: token exchange, tracked at
#: https://github.com/g0rgonus/calsync/issues/1.
#:
#: It used to be offered with a note saying it could not work yet. That was the
#: wrong call: an entry in a dropdown reads as a supported choice however the
#: caption is worded, so the note explained away a problem instead of removing
#: it. Withdrawing it is honest in a way a warning is not, and putting it back
#: is one line once the transport exists.
#:
#: Checked *before* KINDS so a deployment that already stored
#: `target_kind = google` gets this reason rather than "unknown target kind",
#: which would read like a typo.
WITHDRAWN = {
    "google": (
        "the Google target is not available yet. Its payload builder is complete "
        "and tested, but nothing implements Google's OAuth token exchange — "
        "https://github.com/g0rgonus/calsync/issues/1. Set target_kind to "
        "'caldav' in the meantime."
    ),
}


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
    if kind in WITHDRAWN:
        raise TargetError(WITHDRAWN[kind])
    if kind not in KINDS:
        raise TargetError(
            f"unknown target kind {kind!r}; target_kind must be one of "
            f"{', '.join(KINDS)}"
        )

    if kind == "ics_file":
        raise TargetError(
            "target_kind is 'ics_file' but no directory was given; pass --out DIR"
        )

    secrets = secrets or SecretStore()
    password = secrets.get(settings.radicale_secret_ref)
    return build(
        "caldav",
        base_url=settings.radicale_url,
        transport=HttpTransport(username=settings.radicale_user, password=password),
        username=settings.radicale_user,
        password=password,
    )


def describe(conn, *, out_dir: str | None = None) -> str:
    """One line naming where events would go. For status output, not logic."""
    if out_dir:
        return f"ics files in {out_dir}"
    settings = Settings.load(conn)
    if settings.target_kind == "caldav":
        return f"CalDAV at {settings.radicale_url}"
    return settings.target_kind


__all__ = ["KINDS", "WITHDRAWN", "build_target", "describe", "TargetError",
           "SecretError"]
