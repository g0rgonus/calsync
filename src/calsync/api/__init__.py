"""The read half of the calsync API (docs/API.md).

Two endpoints, `GET /v1/events` and `GET /v1/events/{uid}`. They exist because
of the argument in docs/API.md §"Hermes reads through the API, not CalDAV": an
agent handed a read-only Radicale account would have to pull `Nora 🏊 Distance
Set` back apart into a child and an activity, reverse-engineering a string we
generated ourselves, and would re-break every time the naming convention
changed. So structured identity comes out as fields, and the rendered title
comes out beside them clearly labelled as something never to parse.

**Separate from the console, on purpose.** `calsync web` has no login because it
is loopback-only with one human operator, and a password form in front of a page
unreachable without a VPN is a thing to maintain rather than a control. That
reasoning does not transfer: this serves programs, over a token, and the boundary
this project cares about is agent-versus-human rather than inside-versus-outside
the house. Different posture, different app, different port. It fails to start
rather than serve without a token.

**Read-only, and only that.** No proposals, no approvals, no task tokens, no
PATCH. Their consumers — Hermes, the email worker, Matrix inbound — do not
exist, and a review gate with nothing to review cannot be shown to work. What is
here is the half that has something behind it.
"""

from __future__ import annotations

from .app import create_app, serve

__all__ = ["create_app", "serve"]
