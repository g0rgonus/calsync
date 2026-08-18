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

from dataclasses import dataclass

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
        # The *principal*, not the server root. `CaldavTarget` composes
        # `{base}/{collection}/`, and CalDAV collections live under a user —
        # Radicale answers 403 for anything created at the root. `radicale_url`
        # is the server (that is what the console calls it, and what the default
        # `http://localhost:5232` is), so the user belongs here.
        #
        # This was wrong for as long as it existed and no test caught it,
        # because every test built its target by hand with the user already
        # appended. `tests/test_acceptance.py` now goes through this function.
        base_url=f"{settings.radicale_url.rstrip('/')}/{settings.radicale_user}",
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


__all__ = ["KINDS", "WITHDRAWN", "build_target", "describe", "verify", "Check",
           "Finding", "TargetError", "SecretError"]


# --- is the configured calendar actually reachable? -------------------------


@dataclass(frozen=True)
class Finding:
    """One thing that was checked, and what came back."""

    label: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Check:
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.findings) and all(f.ok for f in self.findings)


def verify(conn, secrets: SecretStore | None = None, *, transport=None) -> Check:
    """Ask the configured calendar server whether this configuration is real.

    Written after a deployment sat for days writing nothing, because
    `radicale_url` was `http://localhost:5232` — correct on a laptop and wrong
    inside every container, where localhost is the container itself. The poller
    reported it, once per source per poll, buried in a wall of per-event errors,
    and then backed off to three-hourly. Nothing else asked the question, so
    nobody answered it.

    The failures are separated for the same reason `matrix.verify` separates
    its four: "it didn't work" leaves every cause on the table. Unreachable,
    no credential, rejected credential and no principal are four different
    afternoons.

    Never returns the password and never puts it in a finding — these are
    rendered on a page.
    """
    settings = Settings.load(conn)
    secrets = secrets or SecretStore()

    if settings.target_kind in WITHDRAWN:
        return Check((Finding("Target", False, WITHDRAWN[settings.target_kind]),))
    if settings.target_kind == "ics_file":
        return Check((
            Finding("Target", True,
                    "Writing .ics files, so there is no server to check."),
        ))
    if settings.target_kind != "caldav":
        return Check((
            Finding("Target", False, f"unknown target kind {settings.target_kind!r}"),
        ))

    try:
        password = secrets.get(settings.radicale_secret_ref)
    except SecretError as exc:
        return Check((Finding("Password", False, str(exc)),))

    base = settings.radicale_url.rstrip("/")
    transport = transport or HttpTransport(
        username=settings.radicale_user, password=password
    )
    findings: list[Finding] = []

    try:
        transport("GET", base + "/")
    except TargetError as exc:
        # The one that bit. Named as a hostname problem rather than a generic
        # failure, because from a container the answer is almost always the
        # compose service name.
        hint = ""
        if "localhost" in base or "127.0.0.1" in base:
            hint = (
                " If calsync is running in a container, localhost is that "
                "container — the calendar server is probably at "
                "http://radicale:5232."
            )
        return Check((
            Finding("Server", False, f"{base} did not answer: {exc}.{hint}"),
        ))
    findings.append(Finding("Server", True, f"{base} answered."))

    # The principal, not the root: Radicale answers / for anyone, and a wrong
    # username fails here rather than above.
    try:
        response = transport("PROPFIND", f"{base}/{settings.radicale_user}/",
                             headers={"Depth": "0"})
    except TargetError as exc:
        return Check(tuple(findings) + (
            Finding("Account", False,
                    f"could not reach {settings.radicale_user!r}: {exc}"),
        ))

    if response.status in (401, 403):
        findings.append(Finding(
            "Account", False,
            f"{settings.radicale_user!r} was rejected — check the password "
            f"stored as {settings.radicale_secret_ref!r}.",
        ))
    elif response.status == 404:
        findings.append(Finding(
            "Account", False,
            f"the server has no principal for {settings.radicale_user!r}.",
        ))
    elif 200 <= response.status < 400:
        findings.append(Finding(
            "Account", True,
            f"{settings.radicale_user!r} authenticated and has a collection root.",
        ))
    else:
        findings.append(Finding(
            "Account", False,
            f"the server answered {response.status} for "
            f"{settings.radicale_user!r}.",
        ))
    return Check(tuple(findings))
