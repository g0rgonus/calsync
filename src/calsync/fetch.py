"""Assemble a source URL from its stored template, then fetch it.

Two things this module exists to prevent:

- **Leaking the assembled URL.** Player360 puts the bearer token in the query
  string, so the assembled URL *is* the credential. :class:`AssembledUrl`
  renders redacted under ``str()`` and ``repr()``, which means an accidental log
  line, f-string or traceback cannot expose it. The live value is only reachable
  through the explicit ``.url`` attribute.
- **A frozen ``from`` parameter.** The template carries ``{{now-30d|unix}}``
  rather than a literal timestamp, so the window moves with the clock instead of
  silently drifting into the past (docs/sources/player360.md, trap 6).
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

from .secrets import SecretStore

#: 8 MiB. A season of events is a few hundred KiB; anything at this scale is a
#: wrong endpoint or a hostile response, and should not be buffered further.
MAX_BYTES = 8 << 20
TIMEOUT_S = 30
USER_AGENT = "calsync/0.1 (+https://github.com/g0rgonus/calsync)"

_PLACEHOLDER = re.compile(r"\{\{\s*(?P<body>[^{}]+?)\s*\}\}")
_NOW = re.compile(
    r"^now(?:(?P<sign>[+-])(?P<qty>\d+)(?P<unit>[smhd]))?(?:\|(?P<fmt>unix|iso|date))?$"
)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class FetchError(RuntimeError):
    """A fetch or URL assembly failed.

    Never treated as 'everything was cancelled' — the caller must abort before
    diffing, because no events is indistinguishable from every event gone.
    """


@dataclass(frozen=True)
class AssembledUrl:
    """A URL that may contain credentials.

    ``str()`` and ``repr()`` are redacted on purpose. Reach for ``.url`` only at
    the moment of the request.
    """

    url: str = field(repr=False)
    redacted: str

    def __str__(self) -> str:
        return self.redacted


def render_url(
    template: str, *, secrets: SecretStore, now: datetime
) -> AssembledUrl:
    """Expand ``{{secret:ref}}`` and ``{{now±Nd|fmt}}`` placeholders."""
    revealed: list[str] = []

    def substitute(match: re.Match) -> str:
        body = match.group("body")

        if body.startswith("secret:"):
            value = secrets.get(body[len("secret:") :].strip())
            encoded = quote(value, safe="")
            # Both forms, so redaction catches the value however it was encoded.
            revealed.extend((value, encoded))
            return encoded

        moment = _NOW.match(body)
        if moment is None:
            raise FetchError(f"unknown placeholder {{{{{body}}}}} in url_template")

        stamp = now
        if moment.group("qty"):
            delta = timedelta(
                seconds=int(moment.group("qty")) * _UNIT_SECONDS[moment.group("unit")]
            )
            stamp = now - delta if moment.group("sign") == "-" else now + delta

        fmt = moment.group("fmt") or "unix"
        if fmt == "unix":
            return str(int(stamp.timestamp()))
        if fmt == "date":
            return stamp.strftime("%Y-%m-%d")
        return quote(stamp.isoformat(), safe="")

    url = _PLACEHOLDER.sub(substitute, template)
    # YAML line-continuations leave indentation inside the template; a URL has
    # no legitimate raw whitespace, so collapsing it is safe and fixes the
    # common authoring mistake.
    url = "".join(url.split())

    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchError(f"url_template must resolve to http(s), got {scheme or 'no'} scheme")

    redacted = url
    for value in revealed:
        if value:
            redacted = redacted.replace(value, "***")
    return AssembledUrl(url=url, redacted=redacted)


def http_fetch(assembled: AssembledUrl, *, timeout: int = TIMEOUT_S) -> bytes:
    """GET the assembled URL. Errors quote only the redacted form."""
    request = urllib.request.Request(
        assembled.url, headers={"User-Agent": USER_AGENT, "Accept": "text/calendar, */*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            # Read one byte past the cap so truncation is detectable rather
            # than silently producing a short, parseable-looking feed.
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"GET {assembled.redacted} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"GET {assembled.redacted} failed: {exc}") from exc

    if len(body) > MAX_BYTES:
        raise FetchError(
            f"GET {assembled.redacted} returned more than {MAX_BYTES} bytes; refusing "
            "to parse a possibly truncated feed"
        )
    return body
