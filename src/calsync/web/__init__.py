"""The onboarding console.

**Why Bottle.** The alternatives were the stdlib and Flask. The stdlib gives
``wsgiref`` and nothing else — routing, method dispatch, form decoding, redirects
and escaping would all be hand-rolled here, and escaping is not a thing to
hand-roll. Flask brings five packages to do the same job. Bottle is a single
pure-Python module with no dependencies of its own, ships an autoescaping
template engine, and speaks plain WSGI so this runs under the stdlib server. It
is the smallest thing that covers the job, which is the same argument
``sqlite3``-instead-of-an-ORM makes elsewhere in this package.

**Why direct SQLite.** Configuration is edited in this process rather than
through the calsync API (docs/API.md, "Configuration is not in this API"). That
makes the poller and this app two writers on one file, so:

- every request gets its own connection, opened and closed inside the handler.
  ``db.connect`` sets the busy timeout that stops a config edit during a poll
  from failing immediately;
- no transaction is ever held open across a request.

**Why no login.** Loopback only, one operator, reached through whatever VPN or
authenticating proxy the deployment already has. A password form in front of a
page that is unreachable without one of those is a thing to maintain, not a
control — but that reasoning only holds while the bind address stays loopback.

What *is* checked is ``Sec-Fetch-Site`` on state-changing requests, because a
page in another tab really can post to a loopback URL and nothing else stands
between it and a source being deleted. Deliberately not ``Origin`` against
``Host``: a proxy that rewrites ``Host`` is ordinary, and that check refuses
every legitimate write behind one.
"""

from __future__ import annotations

from .app import create_app, serve

__all__ = ["create_app", "serve"]
