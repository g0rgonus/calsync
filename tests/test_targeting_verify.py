"""Can the configured calendar actually be reached?

Written after a deployment spent days writing nothing because `radicale_url`
was `http://localhost:5232` — correct on a laptop, and wrong inside every
container, where localhost is the container itself. Twelve acceptance tests were
green throughout, because they build their own target from an environment
variable and never read the setting the poller reads.

So these are about the *configured* path, and the first one reproduces the
original bug exactly.
"""

from __future__ import annotations

import pytest

from calsync import db, targeting
from calsync.settings import set_setting
from calsync.targets import TargetError
from calsync.targets.http import Response


class Server:
    """A calendar server that answers however a test needs it to."""

    def __init__(self, *, root=200, principal=200, refuse=False):
        self.root, self.principal, self.refuse = root, principal, refuse
        self.seen = []

    def __call__(self, method, url, *, body=None, headers=None):
        self.seen.append((method, url))
        if self.refuse:
            raise TargetError("[Errno 111] Connection refused")
        status = self.root if url.rstrip("/").endswith("5232") else self.principal
        return Response(status=status, headers={})


class Store:
    def get(self, ref):
        return "hunter2"


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "c.db")
    set_setting(connection, "radicale_url", "http://radicale:5232")
    return connection


def test_an_unreachable_server_is_reported_as_unreachable(conn):
    check = targeting.verify(conn, Store(), transport=Server(refuse=True))

    assert not check.ok
    assert check.findings[0].label == "Server"
    assert "Connection refused" in check.findings[0].detail


def test_a_localhost_url_that_refuses_says_what_it_probably_is(conn):
    """The original bug, and the answer that would have saved a week.

    From a container localhost is the container, so a refused connection to
    localhost is almost always a compose service name away from working. A
    generic "did not answer" is technically true and useless.
    """
    set_setting(conn, "radicale_url", "http://localhost:5232")

    check = targeting.verify(conn, Store(), transport=Server(refuse=True))

    assert not check.ok
    assert "radicale:5232" in check.findings[0].detail, (
        "did not suggest the compose service name"
    )


def test_a_reachable_server_with_a_good_account_passes(conn):
    check = targeting.verify(conn, Store(), transport=Server())

    assert check.ok
    assert [f.label for f in check.findings] == ["Server", "Account"]


def test_a_rejected_password_is_not_reported_as_an_unreachable_server(conn):
    """Four different afternoons, and "it didn't work" leaves all four open."""
    check = targeting.verify(conn, Store(), transport=Server(principal=401))

    assert not check.ok
    server, account = check.findings
    assert server.ok, "blamed the server for a credential problem"
    assert not account.ok
    assert "radicale_password" in account.detail, "did not name the secret to check"


def test_a_missing_principal_is_distinct_from_a_bad_password(conn):
    check = targeting.verify(conn, Store(), transport=Server(principal=404))

    assert "no principal" in check.findings[1].detail


def test_a_missing_password_is_caught_before_the_network(conn):
    class NoSecret:
        def get(self, ref):
            from calsync.secrets import SecretError

            raise SecretError(f"no secret for {ref!r}")

    server = Server()
    check = targeting.verify(conn, NoSecret(), transport=server)

    assert not check.ok
    assert server.seen == [], "went to the network without a credential"


def test_the_ics_target_has_no_server_to_check(conn):
    set_setting(conn, "target_kind", "ics_file")

    check = targeting.verify(conn, Store(), transport=Server(refuse=True))

    assert check.ok
    assert "no server" in check.findings[0].detail


def test_a_withdrawn_target_says_why_rather_than_probing(conn):
    set_setting(conn, "target_kind", "google")

    check = targeting.verify(conn, Store(), transport=Server())

    assert not check.ok
    assert "OAuth" in check.findings[0].detail


def test_the_password_never_reaches_a_finding(conn):
    """These are rendered on a page."""
    check = targeting.verify(conn, Store(), transport=Server(principal=401))

    assert "hunter2" not in " ".join(f.detail for f in check.findings)
