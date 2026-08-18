"""Push a short message to a phone, via Pushover.

Separate from `matrix.py` on purpose, because the two carry different traffic.
Matrix gets the daily digest — something you read when you feel like it. This
gets the handful of things that need you to do something and will otherwise sit
unnoticed for months, which for calsync means exactly one thing today: a season
that has finished.

The credentials are two values, both bearer-ish, so both live in the secret
store with only their *names* in settings — the same arrangement as
`radicale_secret_ref` and `matrix_secret_ref`.

Nothing here retries. A dropped notification about a season that ended in May is
not worth a queue: the condition is still true tomorrow, and the console shows
it whether or not the push arrived.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .secrets import SecretError, SecretStore

API = "https://api.pushover.net/1/messages.json"
TIMEOUT_S = 10

#: Settings keys this module owns. Absent from `Settings`, which is typed access
#: for the sync path — the sync loop has no business seeing push credentials.
KEYS = ("pushover_token_ref", "pushover_user_ref")


class NotifyError(RuntimeError):
    """A push could not be sent. Never carries either credential."""


@dataclass(frozen=True)
class PushoverConfig:
    #: Names of secrets, never the values.
    token_ref: str = "pushover_token"
    user_ref: str = "pushover_user"

    def resolve(self, secrets: SecretStore) -> tuple[str, str]:
        return secrets.get(self.token_ref), secrets.get(self.user_ref)

    def available(self, secrets: SecretStore) -> bool:
        """Both credentials present? Used to skip quietly rather than raise.

        A deployment that has not set Pushover up is not misconfigured, it is
        just not using it, and the poll loop should not log an error every
        twenty minutes about a feature nobody asked for.
        """
        return secrets.has(self.token_ref) and secrets.has(self.user_ref)


def load(conn) -> PushoverConfig:
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(KEYS))})",
            KEYS,
        )
    }
    return PushoverConfig(
        token_ref=rows.get("pushover_token_ref") or "pushover_token",
        user_ref=rows.get("pushover_user_ref") or "pushover_user",
    )


def send(
    config: PushoverConfig,
    secrets: SecretStore,
    message: str,
    *,
    title: str = "calsync",
    url: str | None = None,
    url_title: str | None = None,
    opener=urllib.request.urlopen,
) -> None:
    """Send one notification. Raises rather than returning a status.

    ``url`` becomes a tappable link in the notification, which is the whole
    point of sending one — the message says a season looks finished, and the
    link goes to the page with the button on it.
    """
    try:
        token, user = config.resolve(secrets)
    except SecretError as exc:
        raise NotifyError(str(exc)) from exc

    payload = {"token": token, "user": user, "message": message, "title": title}
    if url:
        payload["url"] = url
        payload["url_title"] = url_title or "Open calsync"

    request = urllib.request.Request(
        API, data=urllib.parse.urlencode(payload).encode(), method="POST"
    )
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = "; ".join(json.loads(exc.read() or b"{}").get("errors", []))
        except ValueError:
            pass
        raise NotifyError(f"Pushover refused it (HTTP {exc.code}){': ' + detail if detail else ''}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NotifyError(f"could not reach Pushover: {exc}") from exc

    if body.get("status") != 1:
        raise NotifyError(f"Pushover refused it: {body}")
