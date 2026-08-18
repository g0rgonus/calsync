"""A real HTTP transport for the CalDAV target.

The target itself takes an injected transport so its interesting logic stays
testable without a server (see targets/caldav.py). This is the production
implementation of that seam, and deliberately nothing more: no retries, no
connection pooling, no redirect following.

Redirects in particular are *disabled*. A 30x on a PUT would silently replay the
body — credentials and all — at a host the server chose, and a calendar write is
not something to repeat somewhere unexpected.
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import TargetError

TIMEOUT_S = 30
#: A season is a few hundred small events; anything larger is a wrong endpoint.
MAX_BYTES = 8 << 20


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = b""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """Basic-auth CalDAV over HTTP(S).

    Plain HTTP is allowed on purpose: docs/deployment/radicale.md specifies a
    Tailscale-only deployment, where the transport is already encrypted and a
    certificate would be ceremony. It is refused for anything else.
    """

    def __init__(self, *, username: str | None = None, password: str | None = None,
                 timeout: int = TIMEOUT_S):
        self.timeout = timeout
        self._auth = None
        if username is not None and password is not None:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self._auth = f"Basic {token}"
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __call__(self, method: str, url: str, *, body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> Response:
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if self._auth:
            request.add_header("Authorization", self._auth)

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return Response(
                    status=response.status,
                    headers={k.title(): v for k, v in response.headers.items()},
                    body=response.read(MAX_BYTES),
                )
        except urllib.error.HTTPError as exc:
            # 4xx/5xx are *results*, not transport failures: the CalDAV target
            # reads 412 as a conflict and 405/409 as "collection exists". Raising
            # here would turn an ETag conflict into an unhandled error.
            return Response(
                status=exc.code,
                headers={k.title(): v for k, v in (exc.headers or {}).items()},
                body=exc.read(MAX_BYTES) if exc.fp else b"",
            )
        except (urllib.error.URLError, OSError) as exc:
            raise TargetError(f"{method} {url} failed: {exc}") from exc
