"""The onboarding console.

Driven through WSGI rather than through the route functions, so the templates
are exercised too — a page that raises at render time is a broken page, and
calling the handler directly would not catch it.

No network. The feed URL is faked at the fetch boundary, which is also the only
place these tests need to lie: everything below it is the real inspection, the
real ``config.apply`` and the real sync loop in dry-run mode.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode

import pytest

from calsync import db, repo
from calsync.fetch import FetchError
from calsync.secrets import SecretStore
from calsync.settings import Settings
from calsync.web import app as web_app

FIXTURES = Path(__file__).parent / "fixtures"
OTTERS = (FIXTURES / "teamreach_otters_sample.ics").read_bytes()
WRENS = (FIXTURES / "teamreach_wrens_sample.ics").read_bytes()

FEED_URL = "https://teamreach.example/ics/9f3c1ab27e4d55c0.ics"

#: Just before the fixture seasons start. Pinned because the sync window decides
#: which events are live at all: run these against the real clock and every feed
#: parses to zero events, every gate condition passes vacuously, and the tests
#: assert nothing.
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


# --- a minimal WSGI client --------------------------------------------------


class Client:
    def __init__(self, app):
        self.app = app

    def _call(self, method, path, body=b"", headers=None):
        from io import BytesIO, StringIO

        path, _, query = path.partition("?")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8730",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.input": BytesIO(body),
            "wsgi.errors": StringIO(),
            "wsgi.url_scheme": "http",
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_HOST": "localhost:8730",
        }
        if body:
            environ["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
        for key, value in (headers or {}).items():
            environ["HTTP_" + key.upper().replace("-", "_")] = value

        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(response_headers)

        chunks = self.app(environ, start_response)
        captured["body"] = b"".join(chunks).decode("utf-8", "replace")
        return captured

    def get(self, path, **kw):
        return self._call("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self._call("POST", path, urlencode(data or {}, doseq=True).encode(), **kw)


@pytest.fixture
def secrets_path(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text("{}")
    path.chmod(0o600)
    return path


class Feed:
    """The network, replaced. Swap ``body`` mid-test to change what a poll sees."""

    def __init__(self, body):
        self.body = body
        self.error = None
        self.fetches = 0

    def __call__(self, assembled, **_kw):
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return self.body


@pytest.fixture
def feed():
    return Feed(OTTERS)


@pytest.fixture
def client(tmp_path, secrets_path, feed):
    """A console over a fresh database, with the network replaced."""
    conn = db.open_db(tmp_path / "calsync.db")
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('parker', 'Parker', 'P', 1);
        """
    )
    conn.commit()
    conn.close()

    app = web_app.create_app(
        tmp_path / "calsync.db",
        secrets=SecretStore(path=secrets_path, environ={}),
        fetcher=feed,
        clock=lambda: NOW,
    )
    return Client(app)


def onboard(client, **overrides):
    """Paste a URL, confirm, create. The whole flow in one call."""
    inspected = client.post("/onboard", {"url": FEED_URL})
    assert inspected["status"] == 200, inspected["body"][:2000]

    form = {
        "url": FEED_URL,
        "team_name": "Otters Spring 2026",
        "child": "parker",
        "sport": "soccer",
        "kind": "teamreach",
        "tz": "America/New_York",
        "token": "Otters",
        "poll_interval_s": "1200",
        "vault": ["path"],
    }
    form.update(overrides)
    return client.post("/onboard/create", form)


# --- the empty console ------------------------------------------------------


def test_the_dashboard_invites_the_first_team(client):
    page = client.get("/")
    assert page["status"] == 200
    assert "No teams yet" in page["body"]


def test_the_paste_page_renders(client):
    page = client.get("/onboard")
    assert page["status"] == 200
    assert "Paste the feed URL" in page["body"]


# --- inspect, without creating anything -------------------------------------


def test_inspecting_a_feed_creates_nothing(client, tmp_path):
    client.post("/onboard", {"url": FEED_URL})

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.list_sources(conn, enabled_only=False) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"] == 0


def test_the_confirm_page_shows_what_the_feed_said(client):
    page = client.post("/onboard", {"url": FEED_URL})["body"]

    assert "Otters Spring 2026" in page       # X-WR-CALNAME
    assert "2026-03-04" in page              # season bounds
    assert "Kingsmere" in page               # venues
    assert "12 games" in page                # counts


def test_an_unreadable_feed_is_refused_without_a_traceback(client, feed):
    feed.body = b"not a calendar"
    page = client.post("/onboard", {"url": FEED_URL})
    assert "Stopped" in page["body"]
    assert "Traceback" not in page["body"]


# --- creating ---------------------------------------------------------------


def test_creating_stages_the_source(client, tmp_path):
    result = onboard(client)
    assert result["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    source = repo.list_sources(conn, enabled_only=False)[0]
    assert source.id == "tr-otters-spring-2026"
    assert source.staging_collection == "onboarding"
    assert source.kind == "teamreach"


def test_the_derived_token_becomes_an_activity_alias(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    activity = repo.list_activities(conn)[0]
    assert "Otters" in activity.aliases, "without this, no fixture resolves"


def test_the_credential_never_reaches_the_database(client, tmp_path, secrets_path):
    onboard(client)

    conn = db.connect(tmp_path / "calsync.db")
    source = repo.list_sources(conn, enabled_only=False)[0]
    assert "9f3c1ab27e4d55c0" not in (source.url_template or "")
    assert "{{secret:" in source.url_template

    # And the whole file, not just that column — an export has to be safe too.
    whole_db = (tmp_path / "calsync.db").read_bytes()
    assert b"9f3c1ab27e4d55c0" not in whole_db

    assert "9f3c1ab27e4d55c0" in json.loads(secrets_path.read_text()).values()


def test_the_stored_template_reassembles_into_the_original_url(client, tmp_path,
                                                              secrets_path):
    from calsync.fetch import render_url

    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source = repo.list_sources(conn, enabled_only=False)[0]

    assembled = render_url(
        source.url_template,
        secrets=SecretStore(path=secrets_path, environ={}),
        now=NOW,
    )
    assert assembled.url == FEED_URL
    assert "9f3c1ab27e4d55c0" not in str(assembled), "redaction leaks the token"


def test_a_url_can_be_stored_verbatim_when_nothing_is_secret(client, tmp_path):
    onboard(client, vault=[])
    conn = db.connect(tmp_path / "calsync.db")
    source = repo.list_sources(conn, enabled_only=False)[0]
    assert source.url_template == FEED_URL


def test_a_second_team_with_the_same_name_gets_its_own_id(client, tmp_path):
    onboard(client)
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    ids = sorted(s.id for s in repo.list_sources(conn, enabled_only=False))
    assert ids == ["tr-otters-spring-2026", "tr-otters-spring-2026-2"]


# --- clone-forward ----------------------------------------------------------


def test_a_second_season_carries_the_settings_that_do_not_churn(client, tmp_path):
    onboard(client, tz="Europe/London")
    onboard(client, team_name="Otters Autumn 2026", tz="")

    conn = db.connect(tmp_path / "calsync.db")
    fresh = next(a for a in repo.list_activities(conn) if a.name == "Otters Autumn 2026")
    assert fresh.tz == "Europe/London", "the timezone should have carried over"


# --- the gate ---------------------------------------------------------------


def test_the_source_page_asks_about_unmatched_fixtures(client, tmp_path):
    """The feed's fixtures are "Ember vs Otters" and we told it the wrong name."""
    onboard(client, token="Meteors", team_name="Meteors")
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    page = client.get(f"/sources/{source_id}")["body"]
    assert "couldn&#039;t be matched" in page or "couldn't be matched" in page
    assert "Otters" in page, "the answer should be offered, not just the problem"


def test_answering_the_question_clears_it(client, tmp_path):
    onboard(client, token="Meteors", team_name="Meteors")
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    assert client.post(f"/sources/{source_id}/alias", {"alias": "Otters"})["status"] == 303

    page = client.get(f"/sources/{source_id}")["body"]
    assert "Every fixture named Meteors on one side." in page


def test_a_practices_only_feed_is_waiting_not_broken(client, feed, tmp_path):
    """The state this design exists for: half-validated and perfectly fine."""
    practices_only = b"\r\n".join(
        [
            b"BEGIN:VCALENDAR",
            b"VERSION:2.0",
            b"PRODID:-//TeamReach//EN",
            b"X-WR-CALNAME:Wrens",
            b"BEGIN:VEVENT",
            b"UID:1@teamreach",
            b"DTSTART:20260305T000000Z",
            b"DTEND:20260305T010000Z",
            b"SUMMARY:Practice",
            b"LOCATION:Larkspur Elementary School",
            b"END:VEVENT",
            b"END:VCALENDAR",
            b"",
        ]
    )
    feed.body = practices_only
    onboard(client, team_name="Wrens", token="")

    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    page = client.get(f"/sources/{source_id}")["body"]

    assert "No games in this feed yet" in page
    assert "Waiting" in page
    assert "Nothing to do" in page


def test_promotion_is_refused_while_the_gate_is_unmet(client, feed, tmp_path):
    feed.body = WRENS
    onboard(client, team_name="Wrens", token="")

    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    result = client.post(f"/sources/{source_id}/promote")
    assert "Not%20promoted" in result["headers"]["Location"]

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_source(conn, source_id).staging_collection == "onboarding"


def test_a_clean_feed_promotes_once_its_venues_are_named(client, feed, tmp_path):
    """The whole flow, end to end: paste, confirm, answer, promote.

    This is the path the console exists to make short, and the only test that
    proves the gate can actually be *satisfied* rather than only refused.
    """
    feed.body = WRENS
    onboard(client, team_name="Wrens", token="")

    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    page = client.get(f"/sources/{source_id}")["body"]
    assert "Your turn" in page, "the venues should be asked about"

    for place in (
        "Larkspur Elementary School",
        "Kingsmere Meadow Park",
        "Kingsmere Meadow Park Soccer Fields",
    ):
        client.post(f"/sources/{source_id}/venue", {"raw": place, "name": place})

    page = client.get(f"/sources/{source_id}")["body"]
    assert "Promote to the real calendars" in page
    assert "Your turn" not in page

    result = client.post(f"/sources/{source_id}/promote")
    assert "Promoted" in result["headers"]["Location"]

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_source(conn, source_id).staging_collection is None


def test_promotion_can_be_forced(client, feed, tmp_path):
    feed.body = WRENS
    onboard(client, team_name="Wrens", token="")

    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    client.post(f"/sources/{source_id}/promote", {"force": "1"})
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_source(conn, source_id).staging_collection is None


def test_naming_a_venue_resolves_it(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    page = client.get(f"/sources/{source_id}")["body"]
    assert "Where is" in page

    client.post(
        f"/sources/{source_id}/venue",
        {"raw": "Kingsmere", "name": "Kingsmere Meadow Park"},
    )

    conn = db.connect(tmp_path / "calsync.db")
    resolved = repo.resolve_venue_alias(conn, "Kingsmere")
    assert resolved is not None and resolved.name == "Kingsmere Meadow Park"
    assert not resolved.pin_confirmed, "a typed name is not a confirmed pin"


def test_a_venue_can_be_an_alias_of_a_known_one(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    client.post(f"/sources/{source_id}/venue",
                {"raw": "Kingsmere", "name": "Kingsmere Meadow Park"})
    client.post(f"/sources/{source_id}/venue",
                {"raw": "Copperfield Athletic Complex", "existing": "Kingsmere Meadow Park"})

    conn = db.connect(tmp_path / "calsync.db")
    assert conn.execute("SELECT COUNT(*) AS n FROM venues").fetchone()["n"] == 1


# --- the dashboard, populated ----------------------------------------------


def test_the_dashboard_lists_a_staged_source(client, tmp_path):
    onboard(client)
    page = client.get("/")["body"]
    assert "Otters Spring 2026" in page
    assert "Parker" in page


def test_the_dashboard_can_skip_the_network(client, feed):
    onboard(client)
    before = feed.fetches
    assert client.get("/?check=0")["status"] == 200
    assert feed.fetches == before, "check=0 fetched anyway"


def test_one_dead_feed_does_not_take_the_dashboard_down(client, feed):
    onboard(client)
    feed.error = OSError("connection refused")
    page = client.get("/")
    assert page["status"] == 200
    assert "feed unreachable" in page["body"]
    assert "Otters Spring 2026" in page["body"], "the card should still be there"


# --- guards -----------------------------------------------------------------


def test_a_cross_site_post_is_rejected(client):
    """What a browser sends when another site's page posts here."""
    result = client.post(
        "/children",
        {"name": "Mallory"},
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert "another site" in result["body"]


def test_a_sibling_port_on_localhost_is_still_rejected(client):
    """Another service on 127.0.0.1 is same-*site* but not same-origin."""
    result = client.post(
        "/children", {"name": "Mallory"}, headers={"Sec-Fetch-Site": "same-site"}
    )
    assert "another site" in result["body"]


def test_a_same_origin_post_is_accepted(client, tmp_path):
    result = client.post(
        "/children",
        {"name": "Mira", "initial": "M"},
        headers={"Origin": "http://localhost:8730", "Sec-Fetch-Site": "same-origin"},
    )
    assert result["status"] == 303
    conn = db.connect(tmp_path / "calsync.db")
    assert any(c.name == "Mira" for c in repo.list_children(conn))


def test_a_proxy_that_rewrites_host_does_not_break_writes(client, tmp_path):
    """The regression this replaced an Origin-vs-Host check to fix.

    Any reverse proxy that terminates at one name and forwards to another —
    nginx, Caddy, Traefik, a tunnel, a VPN front end — makes Origin and Host
    legitimately disagree on every write. The browser still computes
    Sec-Fetch-Site from the URL it is really talking to, so it survives the hop.
    """
    result = client.post(
        "/children",
        {"name": "Mira", "initial": "M"},
        headers={
            "Origin": "https://calsync.tail1234.ts.net",
            "Host": "localhost:8730",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert result["status"] == 303, result["body"][:400]
    conn = db.connect(tmp_path / "calsync.db")
    assert any(c.name == "Mira" for c in repo.list_children(conn))


def test_an_origin_mismatch_names_both_sides(client):
    """Only reachable without Sec-Fetch-Site. A refusal has to be diagnosable."""
    result = client.post(
        "/children",
        {"name": "Mallory"},
        headers={"Origin": "https://calsync.tail1234.ts.net", "Host": "localhost:8730"},
    )
    assert "calsync.tail1234.ts.net" in result["body"]
    assert "localhost:8730" in result["body"]
    assert "--trusted-origin" in result["body"]


def test_the_console_asks_for_an_origin_it_will_recognise(client):
    """`no-referrer` here made every write from an older browser unrecognisable.

    Fetch serializes `Origin` as `null` on a POST whenever the page's referrer
    policy is `no-referrer`, so the console's own forms arrived opaque and the
    Origin fallback — the only check left on a browser that does not send
    Sec-Fetch-Site — refused all of them, suggesting a `--trusted-origin` with
    nothing after it. `same-origin` leaks just as little to an outbound link.
    """
    assert '<meta name="referrer" content="same-origin">' in client.get("/")["body"]


def test_an_opaque_origin_does_not_suggest_an_empty_flag(client):
    result = client.post(
        "/children",
        {"name": "Mallory"},
        headers={"Origin": "null", "Host": "localhost:8730"},
    )
    assert "opaque" in result["body"]
    # The defect this replaces: the mismatch branch's remedy, printed with an
    # empty value after the flag because an opaque origin parses to no host.
    assert "start it with" not in result["body"]


def test_a_trusted_origin_is_accepted_without_sec_fetch_site(tmp_path, secrets_path, feed):
    app = web_app.create_app(
        tmp_path / "calsync.db",
        secrets=SecretStore(path=secrets_path, environ={}),
        fetcher=feed,
        clock=lambda: NOW,
        trusted_origins=("calsync.tail1234.ts.net",),
    )
    result = Client(app).post(
        "/children",
        {"name": "Mira", "initial": "M"},
        headers={"Origin": "https://calsync.tail1234.ts.net", "Host": "localhost:8730"},
    )
    assert result["status"] == 303


def test_a_plain_get_is_never_challenged(client):
    assert client.get("/household", headers={"Sec-Fetch-Site": "cross-site"})["status"] == 200


def test_an_unknown_source_says_so(client):
    page = client.get("/sources/nope")
    assert "no source" in page["body"]


def test_no_page_you_navigate_back_to_carries_a_credential(client, tmp_path):
    """The confirm page is the one exception, and it is not a leak.

    That page carries the URL in a hidden field because the operator pasted it
    into the form on the immediately preceding screen and it has to survive one
    submission. Every page reachable *afterwards* renders from the database,
    where the credential is not.
    """
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    for path in ("/", f"/sources/{source_id}"):
        assert "9f3c1ab27e4d55c0" not in client.get(path)["body"], path


# --- the household ----------------------------------------------------------


def test_the_household_page_lists_kids_and_sports(client):
    page = client.get("/household")["body"]
    assert page.count("Parker") >= 1
    assert "Soccer" in page and "Fencing" in page  # a built-in, and the placeholder


def test_a_second_kid_can_be_added_after_the_first(client, tmp_path):
    """The add form used to render only when there were zero kids."""
    assert client.post("/children", {"name": "Mira", "initial": "M"})["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert {c.name for c in repo.list_children(conn)} == {"Parker", "Mira"}
    assert "Add a kid" in client.get("/household")["body"]


def test_a_kid_can_be_corrected_without_changing_their_id(client, tmp_path):
    """Renaming fixes the calendar titles; it must not orphan the activities."""
    onboard(client)
    client.post("/children", {"id": "parker", "name": "Paddy", "initial": "P"})

    conn = db.connect(tmp_path / "calsync.db")
    child = repo.get_child(conn, "parker")
    assert child.name == "Paddy"
    assert repo.list_activities(conn)[0].child_id == "parker"


def test_nicknames_round_trip(client, tmp_path):
    client.post(
        "/children", {"id": "parker", "name": "Parker", "initial": "P",
                      "nicknames": "Pat, Paddy"}
    )
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_child(conn, "parker").nicknames == ("Pat", "Paddy")
    assert "Pat, Paddy" in client.get("/household")["body"]


def test_two_kids_cannot_share_an_initial(client):
    """A shared title like "P+J" has to mean exactly one pair of kids."""
    result = client.post("/children", {"name": "Pippa", "initial": "P"})
    assert "already another kid" in result["body"]


def test_a_kid_with_a_team_cannot_be_deleted(client, tmp_path):
    """The cascade would discard event_state and strand real calendar events."""
    onboard(client)
    result = client.post("/children/parker/delete")

    assert "still has 1 team" in result["body"]
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_child(conn, "parker").name == "Parker"


def test_a_kid_with_no_teams_can_be_deleted(client, tmp_path):
    client.post("/children", {"name": "Mira", "initial": "M"})
    assert client.post("/children/mira/delete")["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert not any(c.id == "mira" for c in repo.list_children(conn))


def test_a_custom_sport_can_be_added_and_used(client, tmp_path):
    assert client.post("/sports", {"name": "Fencing", "emoji": "🤺"})["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert ("fencing", "🤺") in [(s["id"], s["emoji"]) for s in repo.list_sports(conn)]

    onboard(client, sport="fencing")
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.list_activities(conn)[0].emoji == "🤺"


def test_a_builtin_emoji_can_be_changed_and_survives_a_migration(client, tmp_path):
    """`migrate` re-seeds with INSERT OR IGNORE, so an edit must not be clobbered."""
    client.post("/sports", {"id": "soccer", "name": "Soccer", "emoji": "🥅"})

    db.open_db(tmp_path / "calsync.db").close()  # migrate again

    conn = db.connect(tmp_path / "calsync.db")
    soccer = repo.get_sport(conn, "soccer")
    assert soccer["emoji"] == "🥅"
    assert soccer["builtin"], "editing a built-in should not un-build-in it"


def test_a_builtin_sport_cannot_be_deleted(client):
    result = client.post("/sports/soccer/delete")
    assert "built in" in result["body"]


def test_a_sport_in_use_cannot_be_deleted(client):
    client.post("/sports", {"name": "Fencing", "emoji": "🤺"})
    onboard(client, sport="fencing")
    assert "still used by" in client.post("/sports/fencing/delete")["body"]


def test_save_and_remove_are_not_styled_the_same(client):
    """They were identical, which is how "where do I save?" happens.

    Asserted on the markup rather than left to the eye: the constructive and the
    destructive action on the same card must not render as the same control.
    """
    client.post("/children", {"name": "Mira", "initial": "M"})
    page = client.get("/household")["body"]

    save = 'class="btn" type="submit">Save Mira'
    remove = 'class="btn btn-danger" type="submit">Remove Mira'
    assert save in page
    assert remove in page


def test_saving_returns_to_the_card_that_changed(client):
    """The flash sits at the top of a long page; the anchor goes back to the row."""
    result = client.post("/children", {"id": "parker", "name": "Parker", "initial": "P"})
    assert result["headers"]["Location"].endswith("#parker")


# --- venues -----------------------------------------------------------------


def venue_id(tmp_path, name):
    conn = db.connect(tmp_path / "calsync.db")
    return next(v.id for v in repo.venues_detailed(conn) if v.name == name)


def test_the_venue_list_is_empty_until_a_feed_names_somewhere(client):
    assert "No venues yet" in client.get("/venues")["body"]


def test_a_venue_created_from_a_diagnostic_appears_here(client, tmp_path):
    onboard(client)
    source_id = repo.list_sources(db.connect(tmp_path / "calsync.db"), enabled_only=False)[0].id
    client.post(f"/sources/{source_id}/venue", {"raw": "Kingsmere", "name": "Kingsmere"})

    page = client.get("/venues")["body"]
    assert "Kingsmere" in page


def test_a_venue_is_a_name_and_an_address_and_nothing_else(client, tmp_path):
    """calsync stopped emitting a coordinate pin, so the console stopped
    curating one. A "confirm this pin" control for something nothing writes is
    the same lie as configuration that looks honoured and isn't.
    """
    client.post("/venues", {"name": "Windmere", "address": "1 Windmere Ave"})
    vid = venue_id(tmp_path, "Windmere")
    page = client.get(f"/venues/{vid}")["body"]

    assert "1 Windmere Ave" in page
    for gone in ("Latitude", "Longitude", "unconfirmed", "These are right"):
        assert gone not in page, f"{gone!r} still on the page"


def test_the_location_written_to_a_calendar_is_name_then_address(tmp_path):
    """What actually reaches a phone: one line a maps app can resolve."""
    from datetime import datetime, timezone

    from calsync.models import Activity, Child, Event, Venue
    from calsync.render import render
    from calsync.settings import Settings
    from calsync.targets.ics_file import to_ics

    conn = db.open_db(tmp_path / "settings.db")
    when = datetime(2026, 3, 11, 23, tzinfo=timezone.utc)
    event = Event(uid="u", activity_id="a", starts_at=when, ends_at=when,
                  is_game=True, tz="America/New_York",
                  venue=Venue(raw="Kingsmere", name="Kingsmere Meadow Park",
                              address="1 Kingsmere Rd, Halden VA"))
    rendered = render(
        event,
        Activity(id="a", child_id="c", name="Wrens", sport="soccer",
                 emoji="⚽️", tz="America/New_York"),
        [Child(id="c", name="Mira", initial="M", birth_order=1)],
        Settings.load(conn),
    )
    ics = to_ics(rendered).decode()

    assert "Kingsmere Meadow Park" in ics and "1 Kingsmere Rd" in ics
    assert "GEO:" not in ics
    assert "X-APPLE-STRUCTURED-LOCATION" not in ics


def test_a_field_designator_is_refused_as_a_venue_name(client):
    """"Kingsmere #2" is one field at one park, not a place of its own.

    Folding it in mints a separate venue and a separate pin per field, which is
    exactly what `venue.split_field` exists upstream to prevent.
    """
    result = client.post("/venues", {"name": "Kingsmere #2"})
    assert "is #2 at" in result["body"]
    assert "Kingsmere" in result["body"]


def test_a_plural_fields_name_is_not_mistaken_for_a_designator(client, tmp_path):
    """"Soccer Fields" is what the place is called, not which field you want."""
    assert client.post("/venues", {"name": "Kingsmere Meadow Park Soccer Fields"})["status"] == 303




def test_renaming_keeps_the_old_name_resolving(client, tmp_path):
    """Past events used the old string; dropping it unresolves them all."""
    client.post("/venues", {"name": "Kingsmere"})
    vid = venue_id(tmp_path, "Kingsmere")
    client.post(f"/venues/{vid}", {"name": "Kingsmere Meadow Park"})

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.resolve_venue_alias(conn, "Kingsmere").name == "Kingsmere Meadow Park"
    assert repo.resolve_venue_alias(conn, "Kingsmere Meadow Park") is not None



def test_aliases_can_be_added_and_dropped(client, tmp_path):
    client.post("/venues", {"name": "Kingsmere"})
    vid = venue_id(tmp_path, "Kingsmere")

    client.post(f"/venues/{vid}/alias", {"alias": "Kingsmere#2"})
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.resolve_venue_alias(conn, "Kingsmere#2") is not None

    client.post(f"/venues/{vid}/alias", {"remove": "Kingsmere#2"})
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.resolve_venue_alias(conn, "Kingsmere#2") is None


def test_a_venue_cannot_drop_its_own_name(client, tmp_path):
    client.post("/venues", {"name": "Kingsmere"})
    vid = venue_id(tmp_path, "Kingsmere")
    assert "own name" in client.post(f"/venues/{vid}/alias", {"remove": "Kingsmere"})["body"]


def test_merging_keeps_every_name_from_both(client, tmp_path):
    """The duplicate this list actually grows: one park, three spellings."""
    client.post("/venues", {"name": "Kingsmere"})
    client.post("/venues", {"name": "Kingsmere Meadow Park"})
    losing, winning = venue_id(tmp_path, "Kingsmere"), venue_id(tmp_path, "Kingsmere Meadow Park")
    client.post(f"/venues/{losing}/alias", {"alias": "Kingsmere#2"})

    client.post(f"/venues/{losing}/merge", {"into": str(winning)})

    conn = db.connect(tmp_path / "calsync.db")
    assert len(repo.venues_detailed(conn)) == 1
    for seen in ("Kingsmere", "Kingsmere Meadow Park", "Kingsmere#2"):
        resolved = repo.resolve_venue_alias(conn, seen)
        assert resolved is not None and resolved.name == "Kingsmere Meadow Park", seen


def test_merging_moves_the_home_ground_across(client, tmp_path):
    onboard(client)
    client.post("/venues", {"name": "Kingsmere"})
    client.post("/venues", {"name": "Kingsmere Meadow Park"})
    losing, winning = venue_id(tmp_path, "Kingsmere"), venue_id(tmp_path, "Kingsmere Meadow Park")

    conn = db.connect(tmp_path / "calsync.db")
    activity = repo.list_activities(conn)[0]
    conn.execute("UPDATE activities SET home_venue_id = ? WHERE id = ?", (losing, activity.id))
    conn.commit()

    client.post(f"/venues/{losing}/merge", {"into": str(winning)})

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_activity(conn, activity.id).home_venue == "Kingsmere Meadow Park"


def test_a_venue_cannot_be_merged_into_itself(client, tmp_path):
    client.post("/venues", {"name": "Kingsmere"})
    vid = venue_id(tmp_path, "Kingsmere")
    assert client.post(f"/venues/{vid}/merge", {"into": str(vid)})["status"] == 500

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_venue_detail(conn, vid) is not None


def test_deleting_a_venue_strands_nothing_on_the_calendar(client, tmp_path):
    """Unlike deleting a child: no event_state row references a venue."""
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    client.post(f"/sources/{source_id}/venue", {"raw": "Kingsmere", "name": "Kingsmere"})
    vid = venue_id(tmp_path, "Kingsmere")

    assert client.post(f"/venues/{vid}/delete")["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.resolve_venue_alias(conn, "Kingsmere") is None
    # And the place is simply asked about again, rather than silently lost.
    assert "Where is" in client.get(f"/sources/{source_id}")["body"]


def test_deleting_a_home_ground_says_what_it_cost(client, tmp_path):
    onboard(client)
    client.post("/venues", {"name": "Kingsmere"})
    vid = venue_id(tmp_path, "Kingsmere")
    conn = db.connect(tmp_path / "calsync.db")
    activity = repo.list_activities(conn)[0]
    conn.execute("UPDATE activities SET home_venue_id = ? WHERE id = ?", (vid, activity.id))
    conn.commit()

    result = client.post(f"/venues/{vid}/delete")
    assert "lost its home ground" in result["headers"]["Location"].replace("%20", " ")


# --- settings ---------------------------------------------------------------


def test_the_settings_page_shows_a_worked_title(client):
    """The template fields are the least guessable setting in the table."""
    page = client.get("/settings")["body"]
    assert "Parker" in page and "Chargers" in page


def test_changing_the_title_template_changes_the_worked_example(client):
    client.post("/settings/titles", {"title_template": "{emoji} {activity} {detail}"})
    page = client.get("/settings")["body"]
    assert "⚽️ Meteors vs Chargers" in page


def test_a_broken_title_template_says_so_instead_of_500ing(client):
    """The operator is mid-edit; a stack trace is not an answer."""
    client.post("/settings/titles", {"title_template": "{kids} {nonsense}"})
    page = client.get("/settings")
    assert page["status"] == 200
    assert "template error" in page["body"]


def test_calendar_settings_round_trip(client, tmp_path):
    client.post("/settings/calendar", {"collection_template": "{child}-{type}"})
    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).collection_template == "{child}-{type}"


def test_the_timezone_is_picked_from_a_list_not_typed(client):
    """`EDT` got typed into the free-text box this replaces, and stuck.

    It is not an IANA key, so nothing could load it and every local time fell
    back to UTC — a household on UTC and a household whose timezone is broken
    looked identical.
    """
    page = client.get("/settings")["body"]
    assert '<select name="default_tz"' in page
    assert 'name="default_tz" class="mono"\n               value=' not in page
    assert "America/Toronto" in page


def test_the_offered_timezones_all_handle_daylight_saving(client):
    """City zones only. A fixed-offset name loads and is still wrong for half
    the year, which is the failure nothing reports until a season boundary."""
    from calsync import zones

    offered = zones.offered()
    assert "UTC" in offered
    assert not [n for n in offered if "/" not in n and n != "UTC"], "an abbreviation"
    assert not [n for n in offered if n.startswith("Etc/")]
    assert "America/Toronto" in offered and "America/New_York" in offered


def test_a_timezone_nothing_can_load_is_refused_on_the_way_in(client, tmp_path):
    """Refused where the person who typed it is, not at the next render."""
    before = Settings.load(db.connect(tmp_path / "calsync.db")).default_tz
    page = client.post("/settings/calendar", {"default_tz": "EDT"})["body"]

    assert "not a timezone name" in page
    assert "Traceback" not in page
    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).default_tz == before, "the bad value was stored"


def test_a_real_timezone_is_accepted(client, tmp_path):
    client.post("/settings/calendar", {"default_tz": "America/Toronto"})
    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).default_tz == "America/Toronto"


def test_a_stored_timezone_nothing_can_load_is_flagged_on_the_page(client, tmp_path):
    """`CALSYNC_SETTING_DEFAULT_TZ` can still seed one, and a hand-edited row
    can too. The page has to say so rather than quietly rendering UTC."""
    conn = db.connect(tmp_path / "calsync.db")
    conn.execute("UPDATE settings SET value = 'EDT' WHERE key = 'default_tz'")
    conn.commit()

    page = client.get("/settings")["body"]
    assert "Nothing can load" in page
    assert "fall back to UTC" in page


def test_a_value_that_is_not_offered_stays_selected(client, tmp_path):
    """`EST5EDT` handles its switches, so it is accepted but not offered.
    Rendering the form must not silently move the household off it."""
    from calsync import zones

    assert "EST5EDT" not in zones.offered()
    assert zones.choices("EST5EDT")[0] == "EST5EDT"

    conn = db.connect(tmp_path / "calsync.db")
    conn.execute("UPDATE settings SET value = 'EST5EDT' WHERE key = 'default_tz'")
    conn.commit()
    page = client.get("/settings")["body"]
    assert '<option value="EST5EDT" selected>' in page


def test_onboarding_refuses_a_timezone_nothing_can_load(client, tmp_path):
    result = onboard(client, tz="EDT")
    assert "not a timezone name" in result["body"]

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.list_sources(conn, enabled_only=False) == [], "a source was created"


def test_onboarding_offers_the_same_list(client):
    page = client.post("/onboard", {"url": FEED_URL})["body"]
    assert '<select name="tz"' in page
    assert "America/Toronto" in page


def test_a_new_team_gets_a_timezone_that_resolves(client, tmp_path):
    """The activity's tz is what every event carries, so a bad one here is the
    whole season rendered in the wrong zone."""
    from zoneinfo import ZoneInfo

    onboard(client, tz="America/Toronto")
    conn = db.connect(tmp_path / "calsync.db")
    activity = repo.list_activities(conn)[0]
    assert activity.tz == "America/Toronto"
    ZoneInfo(activity.tz)


def test_a_caldav_password_goes_to_the_secret_store_not_the_database(
    client, tmp_path, secrets_path
):
    client.post("/settings/calendar",
                {"radicale_secret_ref": "radicale_password", "radicale_password": "hunter2"})

    assert json.loads(secrets_path.read_text())["radicale_password"] == "hunter2"
    assert b"hunter2" not in (tmp_path / "calsync.db").read_bytes()
    assert "hunter2" not in client.get("/settings")["body"]


def test_the_disappearance_guard_cannot_be_widened_into_uselessness(client, tmp_path):
    """A guard you can switch off from a web form in two clicks is not a guard."""
    result = client.post("/settings/safety", {"max_disappearance_pct": "100"})
    assert "truncated feed" in result["body"]

    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).max_disappearance_pct == 0.20, "the guard moved"


def test_the_guard_can_still_be_narrowed(client, tmp_path):
    client.post("/settings/safety", {"max_disappearance_pct": "5",
                                     "max_disappearance_count": "2"})
    conn = db.connect(tmp_path / "calsync.db")
    settings = Settings.load(conn)
    assert settings.max_disappearance_pct == 0.05
    assert settings.max_disappearance_count == 2


def test_a_percentage_is_accepted_either_way_round(client, tmp_path):
    """Config already tolerates 20 and 0.20 meaning the same thing."""
    client.post("/settings/safety", {"max_disappearance_pct": "0.15"})
    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).max_disappearance_pct == 0.15


# --- matrix -----------------------------------------------------------------


class FakeMatrix:
    """A homeserver. Records the token it saw so a test can prove it was sent."""

    def __init__(self, whoami="@calsync:example.org", rooms=("!room:example.org",),
                 status=200):
        self.whoami, self.rooms, self.status = whoami, rooms, status
        self.tokens = []

    def __call__(self, request, timeout=None):
        self.tokens.append(request.headers.get("Authorization", ""))
        body = (
            {"user_id": self.whoami}
            if "whoami" in request.full_url
            else {"joined_rooms": list(self.rooms)}
        )
        return _Response(self.status, body)


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def matrix_client(tmp_path, secrets_path, feed):
    server = FakeMatrix()
    app = web_app.create_app(
        tmp_path / "calsync.db",
        secrets=SecretStore(path=secrets_path, environ={}),
        fetcher=feed,
        clock=lambda: NOW,
        matrix_opener=server,
    )
    return Client(app), server


def test_the_matrix_token_goes_to_the_secret_store_not_the_database(
    matrix_client, tmp_path, secrets_path
):
    client, _ = matrix_client
    client.post("/settings/matrix", {
        "matrix_homeserver": "https://matrix.example.org",
        "matrix_user_id": "@calsync:example.org",
        "matrix_room_id": "!room:example.org",
        "matrix_secret_ref": "matrix_access_token",
        "matrix_access_token": "syt_supersecret",
    })

    assert json.loads(secrets_path.read_text())["matrix_access_token"] == "syt_supersecret"
    assert b"syt_supersecret" not in (tmp_path / "calsync.db").read_bytes()
    assert "syt_supersecret" not in client.get("/settings")["body"]


def _configure(client, **overrides):
    form = {
        "matrix_homeserver": "https://matrix.example.org",
        "matrix_user_id": "@calsync:example.org",
        "matrix_room_id": "!room:example.org",
        "matrix_secret_ref": "matrix_access_token",
        "matrix_access_token": "syt_supersecret",
    }
    form.update(overrides)
    client.post("/settings/matrix", form)


def test_verifying_reaches_the_homeserver_with_the_token(matrix_client):
    client, server = matrix_client
    _configure(client)

    page = client.post("/settings/matrix/verify")["body"]

    assert server.tokens == ["Bearer syt_supersecret"] * 2
    assert "Your homeserver accepts all of this" in page
    assert "syt_supersecret" not in page, "the token reached a page"


def test_a_token_belonging_to_another_account_is_caught(matrix_client):
    client, server = matrix_client
    server.whoami = "@someoneelse:example.org"
    _configure(client)

    page = client.post("/settings/matrix/verify")["body"]
    assert "@someoneelse:example.org" in page
    assert "You configured @calsync:example.org" in page


def test_an_account_outside_the_room_is_caught(matrix_client):
    client, server = matrix_client
    server.rooms = ()
    _configure(client)

    page = client.post("/settings/matrix/verify")["body"]
    assert "is not in !room:example.org" in page


def test_an_unreachable_homeserver_is_reported_not_raised(matrix_client):
    client, server = matrix_client
    _configure(client)

    def dead(*_a, **_k):
        raise OSError("name or service not known")

    server.__class__.__call__ = staticmethod(dead)
    page = client.post("/settings/matrix/verify")
    assert page["status"] == 200
    assert "did not answer" in page["body"]


def test_verifying_without_a_token_says_which_secret_is_missing(matrix_client):
    client, _ = matrix_client
    _configure(client, matrix_access_token="", matrix_secret_ref="matrix_missing")
    page = client.post("/settings/matrix/verify")["body"]
    assert "matrix_missing" in page


def test_every_template_field_renders_in_the_worked_example(client):
    """An unpopulated sample makes a working field look broken."""
    client.post("/settings/titles",
                {"title_template": "{kids}|{emoji}|{detail}|{sport}|{activity}|{venue}"})
    page = client.get("/settings")["body"]
    assert "Parker|⚽️|vs Chargers|soccer|Meteors|Kingsmere Meadow Park" in page


def test_every_input_type_the_console_uses_is_styled():
    """A field type missing from the stylesheet renders unstyled and narrow.

    Invisible until that type turns up on a page — which is how the password
    fields shipped looking broken.
    """
    import re
    from pathlib import Path as _Path

    root = _Path(web_app.__file__).parent
    css = (root / "static" / "app.css").read_text()
    styled = set(re.findall(r'input\[type="(\w+)"\]', css))
    used = set()
    for view in (root / "templates").glob("*.tpl"):
        used |= set(re.findall(r'<input[^>]*type="(\w+)"', view.read_text()))

    unstyled = used - styled - {"hidden", "checkbox", "radio"}
    assert not unstyled, f"input types with no styling: {sorted(unstyled)}"


# --- error handling ---------------------------------------------------------


def test_a_non_numeric_field_is_the_operators_problem_not_a_crash(client, tmp_path):
    """These arrive from selects and number inputs, so a bad one is bad input.

    Rendering it as "This is a bug in calsync" over a traceback tells the
    operator the wrong thing about their own console.
    """
    result = client.post("/children", {"id": "parker", "name": "Parker",
                                       "initial": "P", "birth_order": "second"})
    assert "whole number" in result["body"]
    assert "This is a bug in calsync" not in result["body"]
    assert "Traceback" not in result["body"]


def test_a_missing_row_reads_differently_from_a_bug(client):
    """`repo.NotFound` is user error; a stray KeyError is a defect.

    Catching bare KeyError reported real bugs as ordinary user error, which is
    the worst place for a defect to hide.
    """
    from calsync import repo as _repo

    assert issubclass(_repo.NotFound, KeyError)

    page = client.get("/sources/nope")["body"]
    assert "Stopped" in page and "This is a bug" not in page


def test_an_unexpected_error_is_reported_as_a_bug(client, monkeypatch):
    """The other half of that split: a real defect must not read as user error."""
    def boom(*_a, **_k):
        raise KeyError("some_internal_dict_key")

    monkeypatch.setattr(web_app.repo, "list_children", boom)
    page = client.get("/")["body"]
    assert "This is a bug in calsync" in page


# --- the routes nothing exercised ------------------------------------------


def test_a_live_source_can_be_sent_back_to_staging(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    repo.set_staging(conn, source_id, None)

    assert client.post(f"/sources/{source_id}/stage")["status"] == 303
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_source(conn, source_id).staging_collection == "onboarding"


def test_polling_can_be_paused_and_resumed(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    client.post(f"/sources/{source_id}/enabled", {"enabled": "0"})
    conn = db.connect(tmp_path / "calsync.db")
    assert not repo.get_source(conn, source_id).enabled
    assert "paused" in client.get("/")["body"]

    client.post(f"/sources/{source_id}/enabled", {"enabled": "1"})
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_source(conn, source_id).enabled


def test_the_stylesheet_is_served(client):
    """If package-data ever drops it, every page renders unstyled and silently.

    Nothing else would fail — the pages still return 200.
    """
    result = client.get("/static/app.css")
    assert result["status"] == 200
    assert ".gate" in result["body"], "served something, but not the stylesheet"


def test_every_route_is_exercised_by_some_test():
    """Guards the coverage check itself, which a shell version got wrong."""
    import re
    from pathlib import Path as _Path

    app_src = _Path(web_app.__file__).read_text()
    tests_src = _Path(__file__).read_text()
    missing = []
    for _verb, path in re.findall(r'@app\.(get|post)\("([^"]+)"\)', app_src):
        pattern = re.sub(r'<[^>]*>', '[^"\']*', re.escape(path).replace("\\<", "<"))
        pattern = re.sub(r'<[^>]*>', '[^"\']*', pattern)
        if not re.search(pattern, tests_src):
            missing.append(path)
    assert not missing, f"routes no test calls: {missing}"


# --- the calendar -----------------------------------------------------------


def _month_before(month: str) -> str:
    year, number = (int(part) for part in month.split("-"))
    return f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"


def _month_after(month: str) -> str:
    year, number = (int(part) for part in month.split("-"))
    return f"{year + 1}-01" if number == 12 else f"{year}-{number + 1:02d}"


def synced(client, tmp_path, calendar):
    """A source with a season actually written to `calendar`."""
    onboard(client)
    conn = db.open_db(tmp_path / "calsync.db")
    source = repo.list_sources(conn, enabled_only=False)[0]
    client.post(f"/sources/{source.id}/sync")
    return source.id


def test_the_calendar_shows_what_was_written(writing, tmp_path):
    client, calendar = writing
    synced(client, tmp_path, calendar)

    page = client.get("/calendar?month=2026-03")
    assert page["status"] == 200
    assert "March 2026" in page["body"]
    # Titled the way the calendar has it — composed now, through the same
    # `normalize/title.py` the event went through on its way out.
    assert "Parker" in page["body"]

    # Everything that reached the calendar is on some month of this page, and
    # nothing else is. A count for one month would pass just as well if half the
    # season had been dropped on a boundary.
    conn = db.connect(tmp_path / "calsync.db")
    months = sorted(
        row["m"] for row in conn.execute(
            "SELECT DISTINCT substr(starts_at, 1, 7) AS m FROM event_state"
        )
    )
    # A month either side: days are the venue's, so an event stored just after
    # midnight UTC belongs to the previous month locally.
    span = [_month_before(months[0]), *months, _month_after(months[-1])]
    chips = sum(
        client.get(f"/calendar?month={m}")["body"].count('class="chip')
        for m in span
    )
    assert chips == len(calendar.written)


def test_the_calendar_reads_the_receipt_not_the_feed(writing, tmp_path, feed):
    """`event_content`, the same source the digest and the API read.

    A feed that has since changed — or gone away entirely — must not change what
    this page says, because it has not changed what is on anybody's phone.
    """
    client, calendar = writing
    synced(client, tmp_path, calendar)
    before = client.get("/calendar?view=agenda&month=2026-03")["body"]

    feed.error = FetchError("the team app is down")
    assert client.get("/calendar?view=agenda&month=2026-03")["body"] == before
    assert feed.fetches, "sanity: the fixture feed is the one being read"


def test_the_calendar_offers_both_views(writing, tmp_path):
    client, calendar = writing
    synced(client, tmp_path, calendar)

    grid = client.get("/calendar?month=2026-03")["body"]
    agenda = client.get("/calendar?view=agenda&month=2026-03")["body"]
    assert "<table class=\"cal\"" in grid and "agenda-row" not in grid
    assert "agenda-row" in agenda and "<table class=\"cal\"" not in agenda
    # Every chip in the grid points at its row in the agenda.
    assert "#ev-0" in grid and 'id="ev-0"' in agenda


def test_a_month_with_nothing_in_it_says_which_kind_of_nothing(writing, tmp_path):
    """Empty because nothing is on, or empty because it aged out of retention.

    They read identically and mean opposite things — the second is not a gap in
    the calendar, only in what calsync still remembers about it.
    """
    client, calendar = writing
    synced(client, tmp_path, calendar)

    ahead = client.get("/calendar?month=2027-01")["body"]
    assert "no team has an event" in ahead

    behind = client.get("/calendar?month=2020-01")["body"]
    assert "retention window" in behind
    assert "still on the calendar server" in behind


def test_a_held_event_is_marked_on_the_calendar(writing, tmp_path):
    """The enrichment collection is not a family calendar, and the page says so.

    An event calsync could not place is on no-one's phone; showing it beside the
    ones that are, unmarked, would be the console asserting something false.
    """
    client, calendar = writing
    onboard(client)
    conn = db.open_db(tmp_path / "calsync.db")
    source = repo.list_sources(conn, enabled_only=False)[0]
    # Promote it off staging: staging beats enrichment, so a staged source never
    # holds anything for review.
    repo.set_staging(conn, source.id, None)
    conn.commit()
    conn.close()
    client.post(f"/sources/{source.id}/sync")

    conn = db.connect(tmp_path / "calsync.db")
    held = conn.execute(
        "SELECT COUNT(*) AS n FROM event_state WHERE collection = 'enrichment'"
    ).fetchone()["n"]
    page = client.get("/calendar?view=agenda&month=2026-03")["body"]
    if held:
        assert "held for review" in page
    else:
        assert "held for review" not in page


def test_the_calendar_filters_to_one_child(writing, tmp_path):
    client, calendar = writing
    synced(client, tmp_path, calendar)

    page = client.get("/calendar?view=agenda&month=2026-03&child=parker")
    assert page["status"] == 200
    assert "Otters Spring 2026" in page["body"]

    conn = db.connect(tmp_path / "calsync.db")
    conn.execute(
        "INSERT INTO children (id, name, initial, birth_order) "
        "VALUES ('mira', 'Mira', 'M', 2)"
    )
    conn.commit()
    other = client.get("/calendar?view=agenda&month=2026-03&child=mira")["body"]
    assert "Otters Spring 2026" not in other
    assert "Nothing written for March" in other


def test_a_malformed_month_falls_back_rather_than_refusing(writing, tmp_path):
    """It arrives from a link. An error page in place of a calendar helps nobody."""
    client, calendar = writing
    synced(client, tmp_path, calendar)

    page = client.get("/calendar?month=not-a-month")
    assert page["status"] == 200
    assert "Stopped" not in page["body"]


def test_the_calendar_does_not_claim_to_hold_what_calsync_did_not_write(
    writing, tmp_path
):
    """A page called "Calendar" that shows only calsync's own writes has to say so.

    Hand-created family appointments are never touched (docs/MATCHING.md), which
    also means they are never seen — and a view that quietly omits half the
    family's week is one somebody will plan around.
    """
    client, calendar = writing
    synced(client, tmp_path, calendar)
    page = client.get("/calendar?month=2026-03")["body"]
    assert "added to the calendar by hand is not here" in page


# --- syncing on demand ------------------------------------------------------


def test_sync_now_writes_for_real(writing, tmp_path):
    """The point of the button, and the thing a dry run cannot do.

    Every other number on the source page comes from `sync_source(dry_run=True)`
    against a target that refuses to be written to. This one has to reach the
    calendar, so the test checks the calendar rather than a redirect.
    """
    client, calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    assert repo.tracked_events(conn, source_id) == 0

    result = client.post(f"/sources/{source_id}/sync")
    assert result["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert calendar.written, "nothing reached the calendar"
    assert repo.tracked_events(conn, source_id) == len(calendar.written)


def test_sync_now_reports_what_it_did(writing, tmp_path):
    """The report line, not "done". A held guard and a clean poll both redirect."""
    client, calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    where = client.post(f"/sources/{source_id}/sync")["headers"]["Location"]
    assert "ok=" in where
    assert "new" in unquote(where), where


def test_sync_now_is_refused_for_a_paused_source(writing, tmp_path):
    """Retiring cancels every upcoming event and then disables the source.

    A sync that ignored that would write the whole season back, which is the
    console undoing a deliberate act with one click and no warning.
    """
    client, calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    client.post(f"/sources/{source_id}/enabled", {"enabled": "0"})

    page = client.post(f"/sources/{source_id}/sync")["body"]
    assert "paused" in page
    assert not calendar.written, "a paused source was synced anyway"


def test_the_source_page_does_not_offer_to_sync_a_paused_source(writing, tmp_path):
    client, _calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    assert "/sync" in client.get(f"/sources/{source_id}")["body"]

    client.post(f"/sources/{source_id}/enabled", {"enabled": "0"})
    page = client.get(f"/sources/{source_id}")["body"]
    assert f"/sources/{source_id}/sync" not in page
    assert "disabled" in page, "the button should be shown and dead, not hidden"


def test_syncing_everything_covers_every_enabled_source(writing, tmp_path):
    client, calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    assert client.post("/sync")["status"] == 303
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.tracked_events(conn, source_id) > 0


def test_syncing_with_no_teams_says_so_rather_than_claiming_success(client):
    where = client.post("/sync")["headers"]["Location"]
    assert "err=" in where
    assert "nothing to sync" in unquote(where)


def test_a_sync_already_running_is_refused_rather_than_doubled(writing, tmp_path):
    """Two clicks on a slow feed are two requests: the server is threaded.

    The second must not diff against state the first has not committed, so it
    is refused with a reason rather than queued behind it.
    """
    client, calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source = repo.list_sources(conn, enabled_only=False)[0]

    started, release = threading.Event(), threading.Event()
    real_upsert = calendar.upsert

    def slow(event, previous=None):
        started.set()
        release.wait(5)
        return real_upsert(event, previous)

    calendar.upsert = slow
    first = threading.Thread(
        target=lambda: client.post(f"/sources/{source.id}/sync"), daemon=True
    )
    first.start()
    assert started.wait(5), "the first sync never reached the calendar"

    second = client.post(f"/sources/{source.id}/sync")
    release.set()
    first.join(10)

    assert "already syncing" in second["body"]


# --- retiring a season ------------------------------------------------------


class CollectingTarget:
    """A calendar that remembers. Writes so a season can exist, cancels so it
    can be taken away, and records both so a test can check the real thing
    happened rather than that a counter moved."""

    def __init__(self):
        self.written = {}
        self.cancelled = []

    def ensure_collection(self, _collection):
        pass

    def upsert(self, event, _previous=None):
        from calsync.targets import TargetRef

        self.written[event.uid] = event.collection
        return TargetRef(collection=event.collection, remote_id=event.uid,
                         etag='"v1"')

    def cancel(self, ref):
        self.cancelled.append(ref.remote_id)
        self.written.pop(ref.remote_id, None)


@pytest.fixture
def writing(tmp_path, secrets_path, feed):
    """A console wired to a calendar that records, for the two actions that
    reach one: retiring a season and syncing on demand."""
    calendar = CollectingTarget()
    app = web_app.create_app(
        tmp_path / "calsync.db",
        secrets=SecretStore(path=secrets_path, environ={}),
        fetcher=feed,
        clock=lambda: NOW,
        write_target=calendar,
    )
    client = Client(app)
    conn = db.open_db(tmp_path / "calsync.db")
    conn.executescript(
        "INSERT INTO children (id, name, initial, birth_order) "
        "VALUES ('parker', 'Parker', 'P', 1);"
    )
    conn.commit()
    conn.close()
    return client, calendar


def test_the_source_page_offers_to_retire_the_season(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    page = client.get(f"/sources/{source_id}")["body"]
    assert "End of season" in page
    assert "Not a delete" in page


def test_retiring_from_the_console_clears_the_calendar_and_stops_polling(
    writing, tmp_path
):
    client, calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    # Give it something to remove: a real sync through the same target.
    from calsync.sync import sync_source
    sync_source(conn, repo.get_source(conn, source_id), calendar,
                now=NOW, raw=OTTERS)
    live = repo.tracked_events(conn, source_id)
    assert live > 0

    assert client.post(f"/sources/{source_id}/retire")["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert len(calendar.cancelled) == live
    assert calendar.written == {}, "events left on the calendar"
    assert repo.tracked_events(conn, source_id) == 0
    assert not repo.get_source(conn, source_id).enabled


# --- teaching a source what a label means -----------------------------------


#: A label the adapter's own vocabulary does not cover. None of the recorded
#: feeds has one — "Playoff Game2" is already caught by `\bgame\d+\b` — so an
#: honest test of the unknown-type path has to invent the label a coach has not
#: typed yet.
UNKNOWN_TYPE = b"\r\n".join([
    b"BEGIN:VCALENDAR", b"VERSION:2.0", b"PRODID:-//TeamReach//EN",
    b"X-WR-CALNAME:Tempest",
    b"BEGIN:VEVENT", b"UID:1@teamreach", b"DTSTART:20260305T000000Z",
    b"DTEND:20260305T010000Z", b"SUMMARY:Skills Session - Windmere",
    b"END:VEVENT",
    b"BEGIN:VEVENT", b"UID:2@teamreach", b"DTSTART:20260312T000000Z",
    b"DTEND:20260312T010000Z", b"SUMMARY:Game - Windmere",
    b"END:VEVENT",
    b"END:VCALENDAR", b"",
])


def test_an_unknown_event_type_is_answerable_in_the_console(client, feed, tmp_path):
    """It used to say "this needs a code change" and offer nothing."""
    feed.body = UNKNOWN_TYPE
    onboard(client, team_name="Inter Tempest", token="")
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    page = client.get(f"/sources/{source_id}")["body"]
    assert "unrecognised" in page
    assert "is a game" in page and "is a practice" in page


def test_answering_teaches_the_source_and_clears_the_question(client, feed, tmp_path):
    feed.body = UNKNOWN_TYPE
    onboard(client, team_name="Inter Tempest", token="")
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    assert repo.get_source(conn, source_id).config.get("practice_words") is None

    client.post(f"/sources/{source_id}/event-type",
                {"label": "Skills Session", "kind": "practice"})

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_source(conn, source_id).config["practice_words"] == ["Skills Session"]

    # And the question is gone from the page, which is the point of answering.
    page = client.get(f"/sources/{source_id}")["body"]
    assert "unrecognised" not in page
    assert "Every event was classified" in page


def test_a_label_cannot_be_both_a_game_and_a_practice(client, feed, tmp_path):
    """Answering again is a correction, not a second opinion."""
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    client.post(f"/sources/{source_id}/event-type", {"label": "Friendly", "kind": "game"})
    client.post(f"/sources/{source_id}/event-type",
                {"label": "Friendly", "kind": "practice"})

    conn = db.connect(tmp_path / "calsync.db")
    config = repo.get_source(conn, source_id).config
    assert config["practice_words"] == ["Friendly"]
    assert config["game_words"] == []


def test_an_event_is_a_game_or_a_practice_and_nothing_else(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    result = client.post(f"/sources/{source_id}/event-type",
                         {"label": "Friendly", "kind": "banquet"})
    assert "game or a practice" in result["body"]


def test_the_target_can_be_chosen_in_the_console(client, tmp_path):
    page = client.get("/settings")["body"]
    assert "Write events to" in page
    # Withdrawn until the OAuth exchange exists (targeting.WITHDRAWN). Offering
    # it with a caption explaining that it cannot work was worse than not
    # offering it — a dropdown entry reads as supported regardless.
    assert 'value="google"' not in page, "google is offered but cannot work"
    assert "OAuth" not in page, "the page still explains away a choice it no longer offers"

    client.post("/settings/calendar", {"target_kind": "ics_file"})
    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).target_kind == "ics_file"


# --- a season that looks finished -------------------------------------------


def _make_dormant(tmp_path, source_id):
    """A season that finished a hundred days ago, on a feed that still works.

    Deliberately no failed polls: that is the actual shape of an ended rec
    season, and the first version of this detector could not see it.
    """
    from datetime import timedelta

    conn = db.connect(tmp_path / "calsync.db")
    conn.execute("DELETE FROM event_state WHERE source_id = ?", (source_id,))
    conn.execute(
        "INSERT INTO event_state (uid, source_id, collection, content_hash, starts_at)"
        " VALUES ('final', ?, 'games', 'h', ?)",
        (source_id, (NOW - timedelta(days=100)).isoformat()),
    )
    conn.execute("INSERT INTO poll_runs (source_id, status) VALUES (?, 'ok')", (source_id,))
    conn.commit()
    conn.close()


def test_a_finished_season_is_pointed_out_next_to_the_retire_button(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    _make_dormant(tmp_path, source_id)

    page = client.get(f"/sources/{source_id}")["body"]
    assert "This season looks finished" in page
    assert "100 days ago" in page
    assert "Nothing on the calendar has been changed" in page, "must not imply it acted"
    assert "Retire" in page, "the diagnosis should sit beside the answer"


def test_a_finished_season_reads_as_quiet_not_broken(client, tmp_path):
    """It is the expected end of every rec season, not a fault."""
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    _make_dormant(tmp_path, source_id)

    page = client.get("/?check=0")["body"]
    assert "season may be over" in page
    assert "tag-down" not in page


def test_a_healthy_source_is_never_labelled_dormant(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    assert "season may be over" not in client.get("/?check=0")["body"]
    assert "This season looks finished" not in client.get(f"/sources/{source_id}")["body"]


def test_a_source_can_be_marked_as_one_that_comes_back(client, tmp_path):
    """Most teams are replaced yearly; a club team kept across seasons is not.

    Without this it would be switched off every summer and missed every autumn.
    """
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    assert "Keep this one across seasons" in client.get(f"/sources/{source_id}")["body"]

    client.post(f"/sources/{source_id}/persists", {"persists": "1"})

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_source(conn, source_id).config["persists_across_seasons"] is True
    assert "Treat as a single season" in client.get(f"/sources/{source_id}")["body"]


# --- the rest of the settings ------------------------------------------------


def test_every_setting_has_a_control_on_the_page():
    """Guards the drift this test was written after finding.

    Three settings had shipped with no way to reach them from the console, which
    is how "configuration lives in the settings table" quietly becomes
    "configuration lives in the settings table and you edit it with sqlite3".
    """
    import re
    from pathlib import Path as _Path

    from calsync.db import DEFAULT_SETTINGS

    page = (_Path(web_app.__file__).parent / "templates" / "settings.tpl").read_text()
    named = set(re.findall(r'name="(\w+)"', page))
    assert not [k for k in DEFAULT_SETTINGS if k not in named]


def test_the_season_thresholds_are_editable(client, tmp_path):
    client.post("/settings/seasons",
                {"season_nudge_days": "21", "season_shutoff_days": "45"})

    conn = db.connect(tmp_path / "calsync.db")
    settings = Settings.load(conn)
    assert settings.season_nudge_days == 21
    assert settings.season_shutoff_days == 45


def test_a_shutoff_before_the_nudge_is_refused(client, tmp_path):
    """Otherwise a season is switched off before anybody is told about it."""
    result = client.post("/settings/seasons",
                         {"season_nudge_days": "60", "season_shutoff_days": "30"})
    assert "before anybody was told" in result["body"]


def test_the_season_thresholds_actually_drive_the_verdict(client, tmp_path):
    """A setting that reads back but changes nothing is the failure this
    codebase keeps finding; assert the behaviour, not the row."""
    from datetime import timedelta

    from calsync import dormancy

    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    conn.execute("DELETE FROM event_state WHERE source_id = ?", (source_id,))
    conn.execute(
        "INSERT INTO event_state (uid, source_id, collection, content_hash, starts_at)"
        " VALUES ('e', ?, 'games', 'h', ?)",
        (source_id, (NOW - timedelta(days=40)).isoformat()),
    )
    conn.commit()

    assert dormancy.for_source(conn, source_id, now=NOW).stage == dormancy.NUDGE

    client.post("/settings/seasons",
                {"season_nudge_days": "10", "season_shutoff_days": "20"})
    conn = db.connect(tmp_path / "calsync.db")
    assert dormancy.for_source(conn, source_id, now=NOW).stage == dormancy.SHUTOFF


def test_pushover_credentials_go_to_the_secret_store(client, tmp_path, secrets_path):
    client.post("/settings/notifications",
                {"pushover_token": "app-token", "pushover_user": "user-key"})

    stored = json.loads(secrets_path.read_text())
    assert stored["pushover_token"] == "app-token"
    assert stored["pushover_user"] == "user-key"
    assert b"app-token" not in (tmp_path / "calsync.db").read_bytes()
    assert "app-token" not in client.get("/settings")["body"]


def test_the_api_token_goes_to_the_secret_store(client, tmp_path, secrets_path):
    """A bearer token that reads a family's schedule is a credential like any
    other, and the settings table has to stay safe to export."""
    client.post("/settings/api", {"api_token": "hermes-bearer"})

    assert json.loads(secrets_path.read_text())["api_token"] == "hermes-bearer"
    assert b"hermes-bearer" not in (tmp_path / "calsync.db").read_bytes()
    assert "hermes-bearer" not in client.get("/settings")["body"]


def test_the_api_token_can_be_stored_under_a_chosen_name(client, secrets_path):
    client.post(
        "/settings/api", {"api_token_ref": "hermes_token", "api_token": "abc123"}
    )

    assert json.loads(secrets_path.read_text())["hermes_token"] == "abc123"
    assert "hermes_token" in client.get("/settings")["body"]


def test_the_api_section_does_not_imply_a_write_path(client):
    """Proposals, approvals and amendments are specified and not built.

    A settings page describing the product as more capable than it is misleads
    exactly as much as one describing it as less — the same rule the Matrix
    section is held to.
    """
    page = client.get("/settings")["body"]

    assert "GET /v1/events" in page
    assert "Nothing can write through it" in page


def test_a_test_notification_reports_what_happened(tmp_path, secrets_path, feed):
    """These are used a few times a year, so a typo has to surface now."""
    sent = []

    class Server:
        def __call__(self, request, timeout=None):
            sent.append(request.data.decode())
            return _PushReply()

    app = web_app.create_app(
        tmp_path / "calsync.db",
        secrets=SecretStore(path=secrets_path, environ={}),
        fetcher=feed, clock=lambda: NOW, push_opener=Server(),
    )
    client = Client(app)
    client.post("/settings/notifications",
                {"pushover_token": "app-token", "pushover_user": "user-key"})

    page = client.post("/settings/notifications/test")["body"]
    assert "Check your phone" in page
    assert "app-token" in sent[0]
    assert "app-token" not in page, "the token reached a page"


class _PushReply:
    def read(self):
        return b'{"status": 1}'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_the_matrix_section_describes_what_is_actually_built(client):
    """It claimed nothing sent messages, which stopped being true.

    A page describing the product as less capable than it is misleads exactly
    as much as one describing it as more.
    """
    page = client.get("/settings")["body"]
    assert "Nothing sends a Matrix message yet" not in page
    assert "calsync digest --send" in page
    assert "nothing listens" in page


def test_a_digest_time_that_cannot_be_parsed_is_refused(client, tmp_path):
    """`digest.due` reads anything it cannot parse as "never", so a typo would
    otherwise look configured and quietly send nothing all season."""
    result = client.post("/settings/matrix", {"digest_send_at": "half seven"})
    assert "not a time of day" in result["body"]

    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).digest_send_at == ""


def test_a_digest_time_round_trips(client, tmp_path):
    client.post("/settings/matrix", {"digest_send_at": "07:30",
                                     "digest_window_hours": "36"})
    conn = db.connect(tmp_path / "calsync.db")
    settings = Settings.load(conn)
    assert settings.digest_send_at == "07:30"
    assert settings.digest_window_hours == 36


# --- editing a team ---------------------------------------------------------


def test_a_team_can_be_edited_after_onboarding(client, tmp_path):
    """These had no UI at all — they were sqlite3-only after creation."""
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    activity_id = repo.list_activities(conn)[0].id

    client.post(f"/activities/{activity_id}", {
        "name": "Otters", "emoji": "🦅", "official_name": "U10PL",
        "short_name": "Otters", "league": "PSL", "age_group": "U10",
        "alarm_game_min": "120", "alarm_practice_min": "20",
        "back": f"/sources/{source_id}",
    })

    conn = db.connect(tmp_path / "calsync.db")
    activity = repo.get_activity(conn, activity_id)
    assert activity.name == "Otters" and activity.emoji == "🦅"
    assert activity.league == "PSL" and activity.age_group == "U10"
    assert activity.alarm_game_min == 120 and activity.alarm_practice_min == 20


def test_editing_those_fields_changes_how_a_fixture_parses(client, tmp_path):
    """The point of the screen: they feed `known_tokens`, not just the title.

    Asserting the row would miss it — a field that saves and changes no
    behaviour is the failure this codebase keeps turning up.
    """
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    activity_id = repo.list_activities(conn)[0].id
    before = repo.get_activity(conn, activity_id).known_tokens()

    client.post(f"/activities/{activity_id}", {
        "name": "Otters Spring 2026", "official_name": "U10PL", "league": "PSL",
        "alarm_game_min": "90", "alarm_practice_min": "30",
    })

    conn = db.connect(tmp_path / "calsync.db")
    after = repo.get_activity(conn, activity_id).known_tokens()
    assert "U10PL" in after and "PSL" in after
    assert set(after) > set(before), "the parser learned nothing new"


def test_a_home_ground_can_be_set_and_is_what_marks_a_game_away(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    activity_id = repo.list_activities(conn)[0].id
    client.post("/venues", {"name": "Kingsmere Meadow Park"})
    venue = next(v for v in repo.venues_detailed(db.connect(tmp_path / "calsync.db"))
                 if v.name == "Kingsmere Meadow Park")

    client.post(f"/activities/{activity_id}", {
        "name": "Otters", "home_venue_id": str(venue.id),
        "alarm_game_min": "90", "alarm_practice_min": "30",
    })

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_activity(conn, activity_id).home_venue == "Kingsmere Meadow Park"


def test_a_team_cannot_be_left_nameless(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    activity_id = repo.list_activities(conn)[0].id

    assert "needs a name" in client.post(f"/activities/{activity_id}", {"name": "  "})["body"]


def test_upgrading_calsync_does_not_accumulate_version_rows(tmp_path, monkeypatch):
    """It records the current version, not every version ever applied.

    `version` is the primary key, so `INSERT OR REPLACE` collided with nothing
    and appended — leaving 3, 4, 5 in a table whose whole job is to answer "what
    version is this". A bare SELECT then returns the oldest, which is exactly
    backwards in the one situation anybody reads it: working out how far a
    database got before a migration failed.

    The bump has to be simulated. Migrating twice at the *same* version is a
    genuine primary-key collision and stays one row under either implementation,
    so a test that only did that would pass against the bug — which is the shape
    of test this codebase keeps having to throw away.
    """
    conn = db.open_db(tmp_path / "v.db")

    for version in (db.SCHEMA_VERSION + 1, db.SCHEMA_VERSION + 2):
        monkeypatch.setattr(db, "SCHEMA_VERSION", version)
        db.migrate(conn)
        rows = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
        assert rows == [version], f"after upgrading to v{version}: {rows}"


def test_an_older_database_collapses_its_version_history(tmp_path):
    """Databases in the wild already have the accumulated rows."""
    conn = db.open_db(tmp_path / "old.db")
    conn.executemany("INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                     [(1,), (2,), (3,)])
    conn.commit()
    assert len(list(conn.execute("SELECT version FROM schema_version"))) > 1

    db.migrate(conn)

    rows = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
    assert rows == [db.SCHEMA_VERSION]


# --- the review queue -------------------------------------------------------
#
# The half of the Hermes design that works without Hermes: an event calsync
# cannot classify waits in the enrichment calendar, and this is where a human
# answers the question that releases it.


def _sync_once(tmp_path, feed_body=OTTERS, *, promoted=True):
    """A real sync into a real target, so there is something actually held.

    Promoted by default. A staged source routes everything to the onboarding
    calendar, which correctly takes precedence over the enrichment hold — so a
    test that skipped this would assert against a source where the feature
    deliberately does not apply, and pass without exercising it.
    """
    from calsync.sync import sync_source
    from calsync.targets import build

    conn = db.connect(tmp_path / "calsync.db")
    source = repo.list_sources(conn, enabled_only=False)[0]
    if promoted and source.staging_collection:
        repo.set_staging(conn, source.id, None)
        source = repo.get_source(conn, source.id)
    report = sync_source(conn, source, build("ics_file", directory=tmp_path / "out"),
                         now=NOW, raw=feed_body)
    conn.commit()
    conn.close()
    return report


def test_an_unplaceable_event_waits_instead_of_being_filed_as_a_practice(
    client, tmp_path
):
    """The whole point, measured on a real feed.

    The Otters fixture names both sides of every match and neither is
    recognisably ours, so there is no fixture signal and no type word — which
    used to file most of the season under practices. A guess in the wrong
    direction costs a *move* to correct, on events already on other phones.
    """
    onboard(client, token="")            # no alias, so nothing is recognised
    report = _sync_once(tmp_path)

    assert report.awaiting_review > 0, "nothing was held; the fixture proves nothing"
    assert (tmp_path / "out" / "enrichment").is_dir()
    assert not (tmp_path / "out" / "games").exists(), (
        "a fixture we could not identify was filed as a game"
    )


def test_the_review_page_asks_the_question_that_releases_them(client, tmp_path):
    onboard(client, token="")
    _sync_once(tmp_path)

    page = client.get("/review")["body"]

    assert "Otters Spring 2026" in page
    assert "which of these is your team" in page.casefold()
    assert "/alias" in page, "the page states the problem but offers no answer"


def test_answering_on_the_review_page_releases_the_events(client, tmp_path):
    """End to end: the answer moves them onto the real calendar."""
    onboard(client, token="")
    first = _sync_once(tmp_path)
    assert first.awaiting_review > 0

    source_id = repo.list_sources(db.connect(tmp_path / "calsync.db"),
                                  enabled_only=False)[0].id
    client.post(f"/sources/{source_id}/alias", {"alias": "Otters"})

    second = _sync_once(tmp_path)

    assert second.awaiting_review == 0, "the answer did not release anything"
    assert second.moved > 0, "released events should move, not be recreated"
    assert (tmp_path / "out" / "games").is_dir()


def test_the_review_page_is_empty_when_nothing_is_waiting(client, tmp_path):
    onboard(client)                       # token="Otters" — everything resolves
    report = _sync_once(tmp_path)
    assert report.awaiting_review == 0, "setup held events, so this proves nothing"

    page = client.get("/review")["body"]

    assert "Nothing waiting" in page


def test_a_staged_source_is_not_double_held(client, tmp_path):
    """Onboarding already holds everything; enrichment must not shadow it.

    Splitting a source's events across two holding calendars would make the
    promotion gate harder to read rather than safer, so staging wins — and the
    report must not claim a hold that is not happening.
    """
    onboard(client, token="")
    report = _sync_once(tmp_path, promoted=False)

    assert report.staged_to == "onboarding"
    assert report.awaiting_review == 0
    assert not (tmp_path / "out" / "enrichment").exists()
    assert "Nothing waiting" in client.get("/review")["body"]


def test_the_review_page_says_so_when_the_hold_is_switched_off(client, tmp_path):
    """A page that silently does nothing is worse than one that says it is off."""
    onboard(client, token="")
    _sync_once(tmp_path)
    client.post("/settings/calendar", {"enrichment_collection": ""})

    page = client.get("/review")["body"]

    assert "hold is off" in page.casefold()


def test_clearing_the_enrichment_collection_actually_clears_it(client, tmp_path):
    """Blank is a meaningful value here, and the calendar form skips blanks.

    Documented as the way to switch the hold off, so it has to survive the save
    rather than being silently ignored with everything else that was left empty.
    """
    onboard(client)
    client.post("/settings/calendar", {"enrichment_collection": ""})

    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).enrichment_collection == ""


# --- an edit the feed will not explain --------------------------------------


def _flag_edit(tmp_path, uid="x", at="2026-08-20T19:55:21+00:00"):
    conn = db.connect(tmp_path / "calsync.db")
    conn.execute("UPDATE event_state SET upstream_edit_at = ? WHERE uid = ?", (at, uid))
    conn.commit()
    return conn


def test_an_unexplained_edit_is_listed_for_a_human(writing, tmp_path):
    """It is on the calendar and nothing is held, so it is not a question — but
    it is the only notice a cancellation gives, so it goes where somebody looks.
    """
    client, calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    client.post(f"/sources/{source_id}/sync")

    conn = db.connect(tmp_path / "calsync.db")
    uid = conn.execute("SELECT uid FROM event_state LIMIT 1").fetchone()[0]
    _flag_edit(tmp_path, uid)

    page = client.get("/review")["body"]
    assert "Changed at the source" in page
    assert "will not guess at a delete" in page
    assert f"/review/edits/{uid}/seen" in page


def test_marking_an_edit_seen_clears_it_and_nothing_else(writing, tmp_path):
    """Acknowledging is a glance, not a decision. It must not touch the event."""
    client, calendar = writing
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    client.post(f"/sources/{source_id}/sync")

    conn = db.connect(tmp_path / "calsync.db")
    uid = conn.execute("SELECT uid FROM event_state LIMIT 1").fetchone()[0]
    _flag_edit(tmp_path, uid)
    before = dict(conn.execute(
        "SELECT collection, cancelled, content_hash FROM event_state WHERE uid = ?",
        (uid,)).fetchone())

    assert client.post(f"/review/edits/{uid}/seen")["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    row = conn.execute(
        "SELECT collection, cancelled, content_hash, upstream_edit_at "
        "FROM event_state WHERE uid = ?", (uid,)).fetchone()
    assert row["upstream_edit_at"] is None
    assert {k: row[k] for k in before} == before, "acknowledging changed the event"
    assert "Changed at the source" not in client.get("/review")["body"]


def test_a_clean_review_page_does_not_mention_upstream_edits(client):
    assert "Changed at the source" not in client.get("/review")["body"]


# --- deciding on an answer --------------------------------------------------
#
# The only place an answer becomes configuration, and it is reached by a person.
# An agent can put something in front of you and has no path to this handler.


def _answered_task(tmp_path, *, task_type="resolve_activity", answer=None,
                   context=("Ember vs Otters",)):
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    repo.record_task(
        conn, task_id="task_x1", source_id=source_id, kind="unidentified",
        type=task_type, context=context, candidates=("Otters",),
        dispatched_at="2026-03-01T12:00:00+00:00",
    )
    conn.commit()
    repo.record_answer(
        conn, task_id="task_x1", answer=answer or {"alias": "Otters"},
        rationale="Otters is on both sides of every fixture",
        answered_by="hermes/1.4", answered_at="2026-03-01T12:05:00+00:00",
    )
    conn.close()
    return "task_x1", source_id


def test_a_waiting_answer_is_shown_with_who_gave_it(client, tmp_path):
    onboard(client, token="")
    _answered_task(tmp_path)

    page = client.get("/review")["body"]

    assert "Answers waiting on you" in page
    assert "hermes/1.4" in page
    assert "Otters is on both sides" in page, "the rationale is why you can judge it"
    assert "/approve" in page and "/reject" in page


def test_approving_writes_the_same_row_the_manual_form_would(client, tmp_path):
    onboard(client, token="")
    task_id, source_id = _answered_task(tmp_path)

    client.post(f"/review/{task_id}/approve")

    conn = db.connect(tmp_path / "calsync.db")
    activity_id = repo.get_source(conn, source_id).activity_id
    assert "Otters" in repo.get_activity(conn, activity_id).aliases
    assert repo.get_task(conn, task_id).state == repo.APPROVED


def test_rejecting_applies_nothing_and_leaves_the_question_open(client, tmp_path):
    onboard(client, token="")
    task_id, source_id = _answered_task(tmp_path)

    client.post(f"/review/{task_id}/reject")

    conn = db.connect(tmp_path / "calsync.db")
    activity_id = repo.get_source(conn, source_id).activity_id
    assert repo.get_activity(conn, activity_id).aliases == ()
    assert repo.get_task(conn, task_id).state == repo.REJECTED


def test_approving_twice_is_refused(client, tmp_path):
    """The second click of a double-tap must not re-apply anything."""
    onboard(client, token="")
    task_id, _ = _answered_task(tmp_path)
    client.post(f"/review/{task_id}/approve")

    body = client.post(f"/review/{task_id}/approve")["body"]

    assert "nothing waiting on a decision" in body


def test_approving_a_classification_teaches_the_source_its_vocabulary(
    client, tmp_path
):
    """The other answer shape, and it writes to sources.config not an alias."""
    onboard(client, token="")
    task_id, source_id = _answered_task(
        tmp_path, task_type="classify_kind", context=("Skills Session",),
        answer={"label": "Skills Session", "is_game": False},
    )

    client.post(f"/review/{task_id}/approve")

    conn = db.connect(tmp_path / "calsync.db")
    assert "Skills Session" in repo.get_source(conn, source_id).config["practice_words"]


def test_approving_a_venue_answer_leaves_the_pin_unconfirmed(client, tmp_path):
    """A model may propose a place; only a human vouches for coordinates.

    Approving the alias is not vouching for a pin, so `pin_confirmed` stays 0 —
    the invariant that keeps a confident wrong pin off a parent's phone.
    """
    onboard(client, token="")
    task_id, _ = _answered_task(
        tmp_path, task_type="normalize_venue", context=("Kingsmere",),
        answer={"name": "Kingsmere Meadow Park", "address": "1 Kingsmere Rd"},
    )

    client.post(f"/review/{task_id}/approve")

    conn = db.connect(tmp_path / "calsync.db")
    venue = next(v for v in repo.venues_detailed(conn)
                 if v.name == "Kingsmere Meadow Park")
    assert not venue.pin_confirmed
    assert "Kingsmere" in venue.aliases, "the string the feed used was not recorded"


def test_an_answer_that_cannot_be_applied_says_so_and_changes_nothing(
    client, tmp_path
):
    onboard(client, token="")
    task_id, _ = _answered_task(
        tmp_path, task_type="normalize_venue", context=("Kingsmere",),
        answer={"same_as": "A Place That Does Not Exist"},
    )

    body = client.post(f"/review/{task_id}/approve")["body"]

    conn = db.connect(tmp_path / "calsync.db")
    assert "could not apply" in body
    assert repo.get_task(conn, task_id).state == repo.ANSWERED, "decided anyway"


def test_the_calendar_check_is_offered_and_stops_before_the_network(client):
    """No credential is not a server problem, and must not be reported as one.

    Four things go wrong with this configuration and the check separates them,
    for the same reason the Matrix one does — "it didn't work" leaves all four
    on the table.
    """
    assert "Check these against the server" in client.get("/settings")["body"]

    result = client.post("/settings/calendar/verify")["body"]

    assert "Password" in result
    assert "Server" not in result, "blamed the server for a missing credential"


def test_the_calendar_check_names_the_likely_cause_of_a_refused_connection(
    tmp_path, secrets_path, feed
):
    """The button that would have caught a stack writing nothing for days.

    `radicale_url` defaults to localhost, the poller runs in a container where
    localhost is itself, and the failure appeared only as one line per event in
    a log that then backed off to three-hourly. Nothing ever asked the question.

    The transport is injected rather than left to the machine: run this on a box
    where a Radicale happens to be listening on 5232 and the connection
    succeeds, so the test would pass or fail on what else is running.
    """
    from calsync.targets import TargetError

    def refusing(*_a, **_k):
        raise TargetError("[Errno 111] Connection refused")

    db.open_db(tmp_path / "calsync.db").close()
    app = web_app.create_app(
        tmp_path / "calsync.db",
        secrets=SecretStore(path=secrets_path, environ={}),
        fetcher=feed, clock=lambda: NOW, calendar_transport=refusing,
    )
    client = Client(app)
    client.post("/settings/calendar", {"radicale_password": "hunter2"})

    result = client.post("/settings/calendar/verify")["body"]

    assert "did not answer" in result
    assert "radicale:5232" in result, "did not suggest the compose service name"
    assert "hunter2" not in result, "the password reached a page"


def test_init_deploy_writes_a_stack_and_never_clobbers_one(tmp_path):
    """A published image is only half of "you do not need the repo".

    Compose and Radicale's config have to come from somewhere too, so the image
    carries them. Overwriting is refused because these are files somebody edits —
    the rights file especially — and a routine upgrade silently replacing an
    edited one loses a change nobody remembers making.
    """
    from calsync.cli import main

    out = tmp_path / "stack"
    assert main(["init-deploy", str(out)]) == 0
    assert (out / "docker-compose.yml").is_file()
    assert (out / "config" / "radicale" / "config").is_file()
    assert (out / "config" / "radicale" / "rights").is_file()
    # The optional half of the rights file. Shipped whether or not the flag that
    # appends it is set, because a deployment turning anonymous read on later
    # would otherwise find the file its container reads for missing.
    assert (out / "config" / "radicale" / "rights.anonymous").is_file()

    (out / "config" / "radicale" / "rights").write_text("# edited by hand\n")
    assert main(["init-deploy", str(out)]) == 0
    assert (out / "config" / "radicale" / "rights").read_text() == "# edited by hand\n"


# --- which build is this box running ----------------------------------------


def test_every_page_says_which_version_it_is(client):
    """On the shared layout, not on one page.

    The image ships on moving tags, so a browser is often the only thing in
    front of somebody wondering what a deployment actually is. Putting it on
    whatever page they are already looking at is the difference between an
    answer and a shell in a container.
    """
    import calsync

    for path in ("/", "/venues", "/household", "/settings", "/review"):
        body = client.get(path)["body"]
        assert f"calsync {calsync.__version__}" in body, f"no version on {path}"


def test_the_version_is_not_hardcoded_in_the_template(client):
    """A literal in the layout is the drift this whole file exists to prevent."""
    from pathlib import Path

    import calsync

    layout = (Path(calsync.__file__).parent / "web" / "templates" / "layout.tpl")
    assert calsync.__version__ not in layout.read_text()
