"""CalDAV target — Radicale, iCloud, Baikal, Nextcloud, anything that speaks it.

Serialization is shared with the ics_file target: CalDAV is iCalendar over
HTTP. Only the transport differs, and it is injected so the interesting logic
stays testable without a server.
"""

from __future__ import annotations

from typing import Callable, Protocol
from urllib.parse import quote

from ..render import RenderedEvent
from . import Capabilities, TargetError, TargetRef, register
from .ics_file import to_ics


class Response(Protocol):
    status: int
    headers: dict[str, str]


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, *, body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response: ...


@register("caldav")
class CalDavTarget:
    """Write events as one resource per event under ``<base>/<collection>/``.

    ``if_match`` uses the stored ETag so a concurrent edit is a conflict rather
    than a silent overwrite.
    """

    def __init__(
        self,
        base_url: str,
        transport: Transport | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._transport = transport

    def capabilities(self) -> Capabilities:
        return Capabilities(
            structured_location=True,
            custom_properties=True,
            alarms=True,
            cancellation_tombstones=True,
            creates_collections=True,
        )

    def resource_url(self, collection: str, uid: str) -> str:
        return f"{self.base_url}/{quote(collection)}/{quote(uid)}.ics"

    @staticmethod
    def _header(response, name: str) -> str | None:
        """Read a response header without caring how the transport cased it.

        HTTP header names are case-insensitive, and transports normalise them
        differently — urllib's `.title()` turns `ETag` into `Etag`. Looking one
        up by exact spelling silently returned None for every ETag Radicale
        ever sent, so `event_state.remote_etag` was always NULL, every later
        write fell back to `If-None-Match: *`, and every genuine update to an
        existing event failed 412 forever. The unit tests missed it because
        their fake transport spelled the header exactly the way this module
        happened to ask for it.
        """
        headers = getattr(response, "headers", None) or {}
        wanted = name.casefold()
        for key, value in headers.items():
            if key.casefold() == wanted:
                return value
        return None

    def _call(self, method: str, url: str, *, body: bytes | None = None,
              headers: dict[str, str] | None = None):
        if self._transport is None:
            raise TargetError("caldav target has no transport configured")
        return self._transport(method, url, body=body, headers=headers or {})

    def ensure_collection(self, collection: str) -> None:
        url = f"{self.base_url}/{quote(collection)}/"
        response = self._call("MKCALENDAR", url)
        # 405/409 mean it already exists, which is the normal steady state.
        if response.status not in (201, 405, 409):
            raise TargetError(f"could not create collection {collection!r}: {response.status}")

    def upsert(self, event: RenderedEvent, previous: TargetRef | None = None) -> TargetRef:
        moved = previous is not None and previous.collection != event.collection
        if moved:
            # A collection is a distinct URL, so reclassification is
            # delete-then-create. Skipping the delete leaves a ghost copy.
            self.cancel(previous)

        url = self.resource_url(event.collection, event.uid)
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if previous is not None and previous.etag and not moved:
            headers["If-Match"] = previous.etag
        else:
            # A move has just deleted the old resource and is now writing a new
            # URL where nothing exists, so the old ETag cannot match and
            # `If-Match` would fail every time. The precondition that expresses
            # the intent here is "create this", the same as a first write.
            #
            # This was invisible while ETags were being dropped on the floor:
            # `previous.etag` was always empty, so every write took this branch
            # and happened to be right for the wrong reason. Promotion off the
            # onboarding calendar is the path that exercises it.
            headers["If-None-Match"] = "*"

        sequence = 0 if previous is None else 1
        response = self._call("PUT", url, body=to_ics(event, sequence=sequence), headers=headers)

        if response.status == 412:
            raise TargetError(
                f"etag conflict writing {event.uid} — the remote copy changed "
                "since we last saw it; re-read before overwriting"
            )
        if response.status not in (200, 201, 204):
            raise TargetError(f"PUT {url} failed: {response.status}")

        return TargetRef(
            collection=event.collection,
            remote_id=event.uid,
            etag=self._header(response, "ETag"),
        )

    def cancel(self, ref: TargetRef) -> None:
        url = self.resource_url(ref.collection, ref.remote_id)
        headers = {"If-Match": ref.etag} if ref.etag else {}
        response = self._call("DELETE", url, headers=headers)
        if response.status not in (200, 204, 404):
            raise TargetError(f"DELETE {url} failed: {response.status}")
