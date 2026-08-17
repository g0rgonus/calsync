"""Matrix connection settings, and proving they work.

**Nothing in calsync sends a Matrix message yet.** `docs/MATRIX.md` describes a
room where calsync, Hermes and the operator interact; that component does not
exist. This module is deliberately not a client — it stores the four values such
a client would need and then *checks them against the homeserver*, which is the
one useful thing that can be done before the client exists.

Storing them unverified would be the failure this project keeps warning about:
configuration that looks honoured and isn't. A wrong homeserver or an expired
token would sit in the database looking like a working setup until the day
something finally tried to use it. Checking turns it into config that is known
good, so the bot starts from something proven rather than something typed.

The token is a bearer credential and never touches the database. Settings hold
``matrix_secret_ref`` — the *name* of a secret — exactly as ``radicale_secret_ref``
does, and the value lives in the secret store.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .secrets import SecretError, SecretStore

TIMEOUT_S = 10
USER_AGENT = "calsync/0.1 (+https://github.com/g0rgonus/calsync)"

#: Settings keys this module owns. Deliberately absent from :class:`Settings`,
#: which is typed access for the *sync* path — the sync loop has no business
#: seeing chat credentials, and adding them there would put them in its view.
KEYS = ("matrix_homeserver", "matrix_user_id", "matrix_room_id", "matrix_secret_ref")


@dataclass(frozen=True)
class MatrixConfig:
    homeserver: str = ""
    user_id: str = ""
    room_id: str = ""
    #: Name of the secret holding the access token, never the token.
    secret_ref: str = "matrix_access_token"

    @property
    def configured(self) -> bool:
        return bool(self.homeserver and self.user_id)

    @property
    def base(self) -> str:
        return self.homeserver.rstrip("/")


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


def load(conn) -> MatrixConfig:
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(KEYS))})",
            KEYS,
        )
    }
    return MatrixConfig(
        homeserver=rows.get("matrix_homeserver", "") or "",
        user_id=rows.get("matrix_user_id", "") or "",
        room_id=rows.get("matrix_room_id", "") or "",
        secret_ref=rows.get("matrix_secret_ref") or "matrix_access_token",
    )


def _get(url: str, token: str, opener) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def verify(
    config: MatrixConfig,
    secrets: SecretStore,
    *,
    opener=urllib.request.urlopen,
) -> Check:
    """Ask the homeserver whether this configuration is real.

    Four things go wrong with Matrix setup and this separates them: the
    homeserver is unreachable, the token is invalid, the token belongs to a
    different account than the one configured, or the account is not in the
    room. A single "it didn't work" would leave all four on the table.

    Never returns the token, and never puts it in a message — these findings are
    rendered on a page.
    """
    if not config.configured:
        return Check(
            (Finding("Configured", False, "Set a homeserver and a user id first."),)
        )

    try:
        token = secrets.get(config.secret_ref)
    except SecretError as exc:
        return Check((Finding("Access token", False, str(exc)),))

    findings: list[Finding] = []
    try:
        status, body = _get(
            f"{config.base}/_matrix/client/v3/account/whoami", token, opener
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Check(
            (
                Finding("Homeserver", False, f"{config.base} did not answer: {exc}"),
            )
        )

    findings.append(Finding("Homeserver", True, f"{config.base} answered."))

    if status == 401:
        findings.append(
            Finding("Access token", False, "The homeserver rejected it as invalid or expired.")
        )
        return Check(tuple(findings))
    if status != 200:
        findings.append(
            Finding("Access token", False, f"Unexpected HTTP {status} from whoami.")
        )
        return Check(tuple(findings))

    whoami = body.get("user_id", "")
    findings.append(Finding("Access token", True, "Accepted."))
    findings.append(
        Finding(
            "Account",
            whoami == config.user_id,
            f"The token belongs to {whoami or 'an unnamed account'}."
            + ("" if whoami == config.user_id else f" You configured {config.user_id}."),
        )
    )

    if config.room_id:
        try:
            status, body = _get(
                f"{config.base}/_matrix/client/v3/joined_rooms", token, opener
            )
            joined = body.get("joined_rooms", []) if status == 200 else []
            findings.append(
                Finding(
                    "Room",
                    config.room_id in joined,
                    f"{config.room_id} is joined."
                    if config.room_id in joined
                    else f"That account is not in {config.room_id}. Invite it, or "
                    "accept the invitation as that account.",
                )
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            findings.append(Finding("Room", False, f"Could not list rooms: {exc}"))

    return Check(tuple(findings))
