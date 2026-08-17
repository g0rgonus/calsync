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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pytest

from calsync import db, repo
from calsync.secrets import SecretStore
from calsync.settings import Settings
from calsync.web import app as web_app

FIXTURES = Path(__file__).parent / "fixtures"
HAWKS = (FIXTURES / "teamreach_hawks_sample.ics").read_bytes()
COMETS = (FIXTURES / "teamreach_comets_sample.ics").read_bytes()

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
    return Feed(HAWKS)


@pytest.fixture
def client(tmp_path, secrets_path, feed):
    """A console over a fresh database, with the network replaced."""
    conn = db.open_db(tmp_path / "calsync.db")
    conn.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('patrick', 'Patrick', 'P', 1);
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
        "team_name": "Hawks Spring 2026",
        "child": "patrick",
        "sport": "soccer",
        "kind": "teamreach",
        "tz": "America/New_York",
        "token": "Hawks",
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

    assert "Hawks Spring 2026" in page       # X-WR-CALNAME
    assert "2026-03-04" in page              # season bounds
    assert "Riverview" in page               # venues
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
    assert source.id == "tr-hawks-spring-2026"
    assert source.staging_collection == "onboarding"
    assert source.kind == "teamreach"


def test_the_derived_token_becomes_an_activity_alias(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    activity = repo.list_activities(conn)[0]
    assert "Hawks" in activity.aliases, "without this, no fixture resolves"


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
    assert ids == ["tr-hawks-spring-2026", "tr-hawks-spring-2026-2"]


# --- clone-forward ----------------------------------------------------------


def test_a_second_season_carries_the_settings_that_do_not_churn(client, tmp_path):
    onboard(client, tz="Europe/London")
    onboard(client, team_name="Hawks Autumn 2026", tz="")

    conn = db.connect(tmp_path / "calsync.db")
    fresh = next(a for a in repo.list_activities(conn) if a.name == "Hawks Autumn 2026")
    assert fresh.tz == "Europe/London", "the timezone should have carried over"


# --- the gate ---------------------------------------------------------------


def test_the_source_page_asks_about_unmatched_fixtures(client, tmp_path):
    """The feed's fixtures are "Fury vs Hawks" and we told it the wrong name."""
    onboard(client, token="Rockets", team_name="Rockets")
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    page = client.get(f"/sources/{source_id}")["body"]
    assert "couldn&#039;t be matched" in page or "couldn't be matched" in page
    assert "Hawks" in page, "the answer should be offered, not just the problem"


def test_answering_the_question_clears_it(client, tmp_path):
    onboard(client, token="Rockets", team_name="Rockets")
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    assert client.post(f"/sources/{source_id}/alias", {"alias": "Hawks"})["status"] == 303

    page = client.get(f"/sources/{source_id}")["body"]
    assert "Every fixture named Rockets on one side." in page


def test_a_practices_only_feed_is_waiting_not_broken(client, feed, tmp_path):
    """The state this design exists for: half-validated and perfectly fine."""
    practices_only = b"\r\n".join(
        [
            b"BEGIN:VCALENDAR",
            b"VERSION:2.0",
            b"PRODID:-//TeamReach//EN",
            b"X-WR-CALNAME:Comets",
            b"BEGIN:VEVENT",
            b"UID:1@teamreach",
            b"DTSTART:20260305T000000Z",
            b"DTEND:20260305T010000Z",
            b"SUMMARY:Practice",
            b"LOCATION:Sanford Elementary School",
            b"END:VEVENT",
            b"END:VCALENDAR",
            b"",
        ]
    )
    feed.body = practices_only
    onboard(client, team_name="Comets", token="")

    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    page = client.get(f"/sources/{source_id}")["body"]

    assert "No games in this feed yet" in page
    assert "Waiting" in page
    assert "Nothing to do" in page


def test_promotion_is_refused_while_the_gate_is_unmet(client, feed, tmp_path):
    feed.body = COMETS
    onboard(client, team_name="Comets", token="")

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
    feed.body = COMETS
    onboard(client, team_name="Comets", token="")

    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    page = client.get(f"/sources/{source_id}")["body"]
    assert "Your turn" in page, "the venues should be asked about"

    for place in (
        "Sanford Elementary School",
        "Riverview Farm Park",
        "Riverview Farm Park Soccer Fields",
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
    feed.body = COMETS
    onboard(client, team_name="Comets", token="")

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
        {"raw": "Riverview", "name": "Riverview Farm Park"},
    )

    conn = db.connect(tmp_path / "calsync.db")
    resolved = repo.resolve_venue_alias(conn, "Riverview")
    assert resolved is not None and resolved.name == "Riverview Farm Park"
    assert not resolved.pin_confirmed, "a typed name is not a confirmed pin"


def test_a_venue_can_be_an_alias_of_a_known_one(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    client.post(f"/sources/{source_id}/venue",
                {"raw": "Riverview", "name": "Riverview Farm Park"})
    client.post(f"/sources/{source_id}/venue",
                {"raw": "Stoney Run Athletic Complex", "existing": "Riverview Farm Park"})

    conn = db.connect(tmp_path / "calsync.db")
    assert conn.execute("SELECT COUNT(*) AS n FROM venues").fetchone()["n"] == 1


# --- the dashboard, populated ----------------------------------------------


def test_the_dashboard_lists_a_staged_source(client, tmp_path):
    onboard(client)
    page = client.get("/")["body"]
    assert "Hawks Spring 2026" in page
    assert "Patrick" in page


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
    assert "Hawks Spring 2026" in page["body"], "the card should still be there"


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
        {"name": "Millie", "initial": "M"},
        headers={"Origin": "http://localhost:8730", "Sec-Fetch-Site": "same-origin"},
    )
    assert result["status"] == 303
    conn = db.connect(tmp_path / "calsync.db")
    assert any(c.name == "Millie" for c in repo.list_children(conn))


def test_a_proxy_that_rewrites_host_does_not_break_writes(client, tmp_path):
    """The regression this replaced an Origin-vs-Host check to fix.

    Any reverse proxy that terminates at one name and forwards to another —
    nginx, Caddy, Traefik, a tunnel, a VPN front end — makes Origin and Host
    legitimately disagree on every write. The browser still computes
    Sec-Fetch-Site from the URL it is really talking to, so it survives the hop.
    """
    result = client.post(
        "/children",
        {"name": "Millie", "initial": "M"},
        headers={
            "Origin": "https://calsync.tail1234.ts.net",
            "Host": "localhost:8730",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert result["status"] == 303, result["body"][:400]
    conn = db.connect(tmp_path / "calsync.db")
    assert any(c.name == "Millie" for c in repo.list_children(conn))


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
        {"name": "Millie", "initial": "M"},
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
    assert page.count("Patrick") >= 1
    assert "Soccer" in page and "Fencing" in page  # a built-in, and the placeholder


def test_a_second_kid_can_be_added_after_the_first(client, tmp_path):
    """The add form used to render only when there were zero kids."""
    assert client.post("/children", {"name": "Millie", "initial": "M"})["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert {c.name for c in repo.list_children(conn)} == {"Patrick", "Millie"}
    assert "Add a kid" in client.get("/household")["body"]


def test_a_kid_can_be_corrected_without_changing_their_id(client, tmp_path):
    """Renaming fixes the calendar titles; it must not orphan the activities."""
    onboard(client)
    client.post("/children", {"id": "patrick", "name": "Paddy", "initial": "P"})

    conn = db.connect(tmp_path / "calsync.db")
    child = repo.get_child(conn, "patrick")
    assert child.name == "Paddy"
    assert repo.list_activities(conn)[0].child_id == "patrick"


def test_nicknames_round_trip(client, tmp_path):
    client.post(
        "/children", {"id": "patrick", "name": "Patrick", "initial": "P",
                      "nicknames": "Pat, Paddy"}
    )
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_child(conn, "patrick").nicknames == ("Pat", "Paddy")
    assert "Pat, Paddy" in client.get("/household")["body"]


def test_two_kids_cannot_share_an_initial(client):
    """A shared title like "P+J" has to mean exactly one pair of kids."""
    result = client.post("/children", {"name": "Pippa", "initial": "P"})
    assert "already another kid" in result["body"]


def test_a_kid_with_a_team_cannot_be_deleted(client, tmp_path):
    """The cascade would discard event_state and strand real calendar events."""
    onboard(client)
    result = client.post("/children/patrick/delete")

    assert "still has 1 team" in result["body"]
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_child(conn, "patrick").name == "Patrick"


def test_a_kid_with_no_teams_can_be_deleted(client, tmp_path):
    client.post("/children", {"name": "Millie", "initial": "M"})
    assert client.post("/children/millie/delete")["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert not any(c.id == "millie" for c in repo.list_children(conn))


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
    client.post("/children", {"name": "Millie", "initial": "M"})
    page = client.get("/household")["body"]

    save = 'class="btn" type="submit">Save Millie'
    remove = 'class="btn btn-danger" type="submit">Remove Millie'
    assert save in page
    assert remove in page


def test_saving_returns_to_the_card_that_changed(client):
    """The flash sits at the top of a long page; the anchor goes back to the row."""
    result = client.post("/children", {"id": "patrick", "name": "Patrick", "initial": "P"})
    assert result["headers"]["Location"].endswith("#patrick")


# --- venues -----------------------------------------------------------------


def venue_id(tmp_path, name):
    conn = db.connect(tmp_path / "calsync.db")
    return next(v.id for v in repo.venues_detailed(conn) if v.name == name)


def test_the_venue_list_is_empty_until_a_feed_names_somewhere(client):
    assert "No venues yet" in client.get("/venues")["body"]


def test_a_venue_created_from_a_diagnostic_appears_here(client, tmp_path):
    onboard(client)
    source_id = repo.list_sources(db.connect(tmp_path / "calsync.db"), enabled_only=False)[0].id
    client.post(f"/sources/{source_id}/venue", {"raw": "Riverview", "name": "Riverview"})

    page = client.get("/venues")["body"]
    assert "Riverview" in page


def test_a_venue_is_a_name_and_an_address_and_nothing_else(client, tmp_path):
    """calsync stopped emitting a coordinate pin, so the console stopped
    curating one. A "confirm this pin" control for something nothing writes is
    the same lie as configuration that looks honoured and isn't.
    """
    client.post("/venues", {"name": "Passage", "address": "1 Passage Ave"})
    vid = venue_id(tmp_path, "Passage")
    page = client.get(f"/venues/{vid}")["body"]

    assert "1 Passage Ave" in page
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
                  venue=Venue(raw="Riverview", name="Riverview Farm Park",
                              address="1 Riverview Rd, Newport News VA"))
    rendered = render(
        event,
        Activity(id="a", child_id="c", name="Comets", sport="soccer",
                 emoji="⚽️", tz="America/New_York"),
        [Child(id="c", name="Millie", initial="M", birth_order=1)],
        Settings.load(conn),
    )
    ics = to_ics(rendered).decode()

    assert "Riverview Farm Park" in ics and "1 Riverview Rd" in ics
    assert "GEO:" not in ics
    assert "X-APPLE-STRUCTURED-LOCATION" not in ics


def test_a_field_designator_is_refused_as_a_venue_name(client):
    """"Riverview #2" is one field at one park, not a place of its own.

    Folding it in mints a separate venue and a separate pin per field, which is
    exactly what `venue.split_field` exists upstream to prevent.
    """
    result = client.post("/venues", {"name": "Riverview #2"})
    assert "is #2 at" in result["body"]
    assert "Riverview" in result["body"]


def test_a_plural_fields_name_is_not_mistaken_for_a_designator(client, tmp_path):
    """"Soccer Fields" is what the place is called, not which field you want."""
    assert client.post("/venues", {"name": "Riverview Farm Park Soccer Fields"})["status"] == 303




def test_renaming_keeps_the_old_name_resolving(client, tmp_path):
    """Past events used the old string; dropping it unresolves them all."""
    client.post("/venues", {"name": "Riverview"})
    vid = venue_id(tmp_path, "Riverview")
    client.post(f"/venues/{vid}", {"name": "Riverview Farm Park"})

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.resolve_venue_alias(conn, "Riverview").name == "Riverview Farm Park"
    assert repo.resolve_venue_alias(conn, "Riverview Farm Park") is not None



def test_aliases_can_be_added_and_dropped(client, tmp_path):
    client.post("/venues", {"name": "Riverview"})
    vid = venue_id(tmp_path, "Riverview")

    client.post(f"/venues/{vid}/alias", {"alias": "Riverview#2"})
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.resolve_venue_alias(conn, "Riverview#2") is not None

    client.post(f"/venues/{vid}/alias", {"remove": "Riverview#2"})
    conn = db.connect(tmp_path / "calsync.db")
    assert repo.resolve_venue_alias(conn, "Riverview#2") is None


def test_a_venue_cannot_drop_its_own_name(client, tmp_path):
    client.post("/venues", {"name": "Riverview"})
    vid = venue_id(tmp_path, "Riverview")
    assert "own name" in client.post(f"/venues/{vid}/alias", {"remove": "Riverview"})["body"]


def test_merging_keeps_every_name_from_both(client, tmp_path):
    """The duplicate this list actually grows: one park, three spellings."""
    client.post("/venues", {"name": "Riverview"})
    client.post("/venues", {"name": "Riverview Farm Park"})
    losing, winning = venue_id(tmp_path, "Riverview"), venue_id(tmp_path, "Riverview Farm Park")
    client.post(f"/venues/{losing}/alias", {"alias": "Riverview#2"})

    client.post(f"/venues/{losing}/merge", {"into": str(winning)})

    conn = db.connect(tmp_path / "calsync.db")
    assert len(repo.venues_detailed(conn)) == 1
    for seen in ("Riverview", "Riverview Farm Park", "Riverview#2"):
        resolved = repo.resolve_venue_alias(conn, seen)
        assert resolved is not None and resolved.name == "Riverview Farm Park", seen


def test_merging_moves_the_home_ground_across(client, tmp_path):
    onboard(client)
    client.post("/venues", {"name": "Riverview"})
    client.post("/venues", {"name": "Riverview Farm Park"})
    losing, winning = venue_id(tmp_path, "Riverview"), venue_id(tmp_path, "Riverview Farm Park")

    conn = db.connect(tmp_path / "calsync.db")
    activity = repo.list_activities(conn)[0]
    conn.execute("UPDATE activities SET home_venue_id = ? WHERE id = ?", (losing, activity.id))
    conn.commit()

    client.post(f"/venues/{losing}/merge", {"into": str(winning)})

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_activity(conn, activity.id).home_venue == "Riverview Farm Park"


def test_a_venue_cannot_be_merged_into_itself(client, tmp_path):
    client.post("/venues", {"name": "Riverview"})
    vid = venue_id(tmp_path, "Riverview")
    assert client.post(f"/venues/{vid}/merge", {"into": str(vid)})["status"] == 500

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.get_venue_detail(conn, vid) is not None


def test_deleting_a_venue_strands_nothing_on_the_calendar(client, tmp_path):
    """Unlike deleting a child: no event_state row references a venue."""
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    client.post(f"/sources/{source_id}/venue", {"raw": "Riverview", "name": "Riverview"})
    vid = venue_id(tmp_path, "Riverview")

    assert client.post(f"/venues/{vid}/delete")["status"] == 303

    conn = db.connect(tmp_path / "calsync.db")
    assert repo.resolve_venue_alias(conn, "Riverview") is None
    # And the place is simply asked about again, rather than silently lost.
    assert "Where is" in client.get(f"/sources/{source_id}")["body"]


def test_deleting_a_home_ground_says_what_it_cost(client, tmp_path):
    onboard(client)
    client.post("/venues", {"name": "Riverview"})
    vid = venue_id(tmp_path, "Riverview")
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
    assert "Patrick" in page and "Strikers" in page


def test_changing_the_title_template_changes_the_worked_example(client):
    client.post("/settings/titles", {"title_template": "{emoji} {activity} {detail}"})
    page = client.get("/settings")["body"]
    assert "⚽️ Rockets vs Strikers" in page


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


def test_the_matrix_section_says_nothing_uses_it_yet(client):
    """Config that looks honoured and isn't is the failure this avoids."""
    assert "Nothing sends a Matrix message yet" in client.get("/settings")["body"]


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
    assert "Patrick|⚽️|vs Strikers|soccer|Rockets|Riverview Farm Park" in page


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
    result = client.post("/children", {"id": "patrick", "name": "Patrick",
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
def retiring(tmp_path, secrets_path, feed):
    calendar = CollectingTarget()
    app = web_app.create_app(
        tmp_path / "calsync.db",
        secrets=SecretStore(path=secrets_path, environ={}),
        fetcher=feed,
        clock=lambda: NOW,
        retire_target=calendar,
    )
    client = Client(app)
    conn = db.open_db(tmp_path / "calsync.db")
    conn.executescript(
        "INSERT INTO children (id, name, initial, birth_order) "
        "VALUES ('patrick', 'Patrick', 'P', 1);"
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
    retiring, tmp_path
):
    client, calendar = retiring
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    # Give it something to remove: a real sync through the same target.
    from calsync.sync import sync_source
    sync_source(conn, repo.get_source(conn, source_id), calendar,
                now=NOW, raw=HAWKS)
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
    b"X-WR-CALNAME:Hurricanes",
    b"BEGIN:VEVENT", b"UID:1@teamreach", b"DTSTART:20260305T000000Z",
    b"DTEND:20260305T010000Z", b"SUMMARY:Skills Session - Passage",
    b"END:VEVENT",
    b"BEGIN:VEVENT", b"UID:2@teamreach", b"DTSTART:20260312T000000Z",
    b"DTEND:20260312T010000Z", b"SUMMARY:Game - Passage",
    b"END:VEVENT",
    b"END:VCALENDAR", b"",
])


def test_an_unknown_event_type_is_answerable_in_the_console(client, feed, tmp_path):
    """It used to say "this needs a code change" and offer nothing."""
    feed.body = UNKNOWN_TYPE
    onboard(client, team_name="Inter Hurricanes", token="")
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id

    page = client.get(f"/sources/{source_id}")["body"]
    assert "unrecognised" in page
    assert "is a game" in page and "is a practice" in page


def test_answering_teaches_the_source_and_clears_the_question(client, feed, tmp_path):
    feed.body = UNKNOWN_TYPE
    onboard(client, team_name="Inter Hurricanes", token="")
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
    assert "OAuth" in page, "google is offered without saying it cannot work yet"

    client.post("/settings/calendar", {"target_kind": "ics_file"})
    conn = db.connect(tmp_path / "calsync.db")
    assert Settings.load(conn).target_kind == "ics_file"


# --- a season that looks finished -------------------------------------------


def _make_dormant(tmp_path, source_id):
    """Six failed polls and a last success six weeks ago."""
    from datetime import timedelta

    conn = db.connect(tmp_path / "calsync.db")
    for _ in range(6):
        conn.execute("INSERT INTO poll_runs (source_id, status) VALUES (?, 'error')",
                     (source_id,))
    conn.execute("UPDATE sources SET last_success_at = ? WHERE id = ?",
                 ((NOW - timedelta(days=42)).isoformat(), source_id))
    conn.execute("DELETE FROM event_state WHERE source_id = ?", (source_id,))
    conn.commit()
    conn.close()


def test_a_finished_season_is_pointed_out_next_to_the_retire_button(client, tmp_path):
    onboard(client)
    conn = db.connect(tmp_path / "calsync.db")
    source_id = repo.list_sources(conn, enabled_only=False)[0].id
    _make_dormant(tmp_path, source_id)

    page = client.get(f"/sources/{source_id}")["body"]
    assert "This season looks finished" in page
    assert "nothing has been changed" in page, "must not imply it acted"
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
