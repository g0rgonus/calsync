"""Targets are pluggable, and the differences between them are real.

Radicale is one option, not the design. These tests write the same event to a
directory of .ics files, to CalDAV, and to Google, and assert each produces
its own correct wire format rather than a translation of another's.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from calsync import targets
from calsync.db import open_db
from calsync.models import Activity, Child, Event, Venue
from calsync.render import render
from calsync.settings import Settings
from calsync.targets import TargetError, TargetRef
from calsync.targets.caldav import CalDavTarget
from calsync.targets.google import GoogleCalendarTarget, google_event_id, to_google_event
from calsync.targets.ics_file import IcsFileTarget, to_ics


def unfold(ics: bytes | str) -> str:
    """Undo RFC 5545 line folding so assertions can match whole values.

    Folding at 75 octets is correct output, not a defect — but it splits long
    long property values across lines.
    """
    text = ics.decode() if isinstance(ics, bytes) else ics
    return text.replace("\r\n ", "").replace("\n ", "")

VANGUARD = Activity(
    id="jesse-soccer-vanguard", child_id="jesse", name="Vanguard", official_name="U10PL",
    league="PSL", age_group="U10", home_venue="Thistledown Park",
    sport="soccer", emoji="⚽️", tz="America/New_York",
)
JESSE = Child(id="jesse", name="Jesse", initial="J", birth_order=2)


@pytest.fixture()
def settings(tmp_path):
    return Settings.load(open_db(tmp_path / "t.db"))


@pytest.fixture()
def event():
    return Event(
        uid="360Player-event-4823901",
        activity_id="jesse-soccer-vanguard",
        starts_at=datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc),
        is_game=True,
        tz="America/New_York",
        opponent="Harbour FC",
        home=True,
        venue=Venue(
            raw="Thistledown Park 1009 Thistledown Rd, Marbury NX 40114",
            name="Thistledown Park",
            address="1009 Thistledown Rd, Marbury NX 40114",
            lat=37.2308, lon=-76.5197, pin_confirmed=True,
        ),
        url="https://app.360player.com/organization/100200/events/4823901",
        source_id="p360-jesse-vanguard",
        content_hash="abc123",
    )


@pytest.fixture()
def rendered(event, settings):
    return render(event, VANGUARD, [JESSE], settings, alarm_minutes=90)


# --- registry ---------------------------------------------------------------


def test_all_three_targets_are_registered():
    assert set(targets.available()) == {"caldav", "google", "ics_file"}


def test_unknown_target_kind_is_a_clear_error():
    with pytest.raises(TargetError, match="unknown target kind"):
        targets.build("outlook")


def test_capabilities_differ_and_are_declared_not_assumed():
    ics = targets.build("ics_file", directory="/tmp/x").capabilities()
    goog = targets.build("google", calendar_map={}).capabilities()
    assert goog.custom_properties is True     # extendedProperties.private
    assert goog.creates_collections is False  # calendars made out of band


# --- rendering is target-neutral --------------------------------------------


def test_render_produces_a_domain_object_not_ics(rendered):
    assert rendered.title == "Jesse ⚽️ vs Harbour FC"
    assert rendered.collection == "games"
    assert rendered.has_coordinates
    assert "BEGIN:VEVENT" not in rendered.body


def test_body_always_states_venue_local_time(rendered):
    # 14:00Z is 10:00 EDT; a reader in another timezone sees their own clock
    # in the client, so the body has to say which is which.
    assert "Start 10:00 EDT" in rendered.body


def test_body_includes_address_and_source(rendered):
    assert "1009 Thistledown Rd, Marbury NX 40114" in rendered.body
    assert "p360-jesse-vanguard" in rendered.body


def test_body_omits_absent_optional_fields(rendered):
    assert "Kit:" not in rendered.body
    assert "Arrive:" not in rendered.body


# --- ics / caldav serialization ---------------------------------------------


def test_ics_gives_the_location_as_text_a_maps_app_can_resolve(rendered):
    """Venue name and street address, nothing more.

    calsync used to also emit GEO and X-APPLE-STRUCTURED-LOCATION for an exact
    pin. It no longer does: a name and an address are enough for a maps app to
    give a tappable, correct destination, and the exact-pin route cost a
    coordinate round-trip that Radicale silently truncated (docs/deployment/
    radicale.md R5) — a confidently wrong pin instead of a working address.
    """
    ics = to_ics(rendered).decode()

    assert "LOCATION:" in ics
    assert "Thistledown Park" in ics
    assert "GEO:" not in ics
    assert "X-APPLE-STRUCTURED-LOCATION" not in ics


def test_ics_has_alarm_from_the_activity_policy(rendered):
    ics = unfold(to_ics(rendered))
    assert "BEGIN:VALARM" in ics
    assert "TRIGGER:-PT1H30M" in ics


def test_ics_never_carries_upstream_sequence(rendered):
    """Upstream SEQUENCE churns when an event merely ends; ours must not."""
    assert "SEQUENCE:0" in unfold(to_ics(rendered, sequence=0))
    assert "SEQUENCE:1" in unfold(to_ics(rendered, sequence=1))


def test_ics_carries_provenance(rendered):
    ics = unfold(to_ics(rendered))
    assert "X-CALSYNC-SOURCE:p360-jesse-vanguard" in ics
    assert "X-CALSYNC-HASH:abc123" in ics


def test_cancellation_is_a_tombstone_not_a_purge(rendered):
    tomb = replace(rendered, cancelled=True)
    assert "STATUS:CANCELLED" in unfold(to_ics(tomb))


def test_ics_file_target_round_trip(tmp_path, rendered):
    target = IcsFileTarget(tmp_path)
    ref = target.upsert(rendered)
    assert ref.collection == "games"
    path = tmp_path / "games" / "360Player-event-4823901.ics"
    assert path.exists()
    assert b"Jesse" in path.read_bytes()

    target.cancel(ref)
    assert not path.exists()


def test_collection_change_moves_rather_than_duplicating(tmp_path, rendered):
    """A collection is a distinct location; an update would leave a ghost."""
    target = IcsFileTarget(tmp_path)
    ref = target.upsert(rendered)

    moved = replace(rendered, collection="practices")
    target.upsert(moved, previous=ref)

    assert not (tmp_path / "games" / "360Player-event-4823901.ics").exists()
    assert (tmp_path / "practices" / "360Player-event-4823901.ics").exists()


def test_move_required_detects_reclassification(rendered):
    same = TargetRef(collection="games", remote_id="x")
    other = TargetRef(collection="practices", remote_id="x")
    assert targets.move_required(same, rendered) is False
    assert targets.move_required(other, rendered) is True
    assert targets.move_required(None, rendered) is False


# --- caldav transport -------------------------------------------------------


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, body=None, headers=None):
        self.calls.append((method, url, body, headers or {}))
        return self.responses.pop(0) if self.responses else FakeResponse(201)


def test_caldav_put_uses_if_none_match_on_create(rendered):
    t = FakeTransport(FakeResponse(201, {"ETag": '"v1"'}))
    ref = CalDavTarget("http://dav.local/calsync", transport=t).upsert(rendered)
    method, url, body, headers = t.calls[0]
    assert method == "PUT"
    assert url.endswith("/games/360Player-event-4823901.ics")
    assert headers["If-None-Match"] == "*"
    assert ref.etag == '"v1"'


def test_caldav_put_uses_if_match_on_update(rendered):
    t = FakeTransport(FakeResponse(204, {"ETag": '"v2"'}))
    prev = TargetRef(collection="games", remote_id=rendered.uid, etag='"v1"')
    CalDavTarget("http://dav.local", transport=t).upsert(rendered, previous=prev)
    assert t.calls[0][3]["If-Match"] == '"v1"'


def test_caldav_etag_conflict_is_raised_not_overwritten(rendered):
    t = FakeTransport(FakeResponse(412))
    prev = TargetRef(collection="games", remote_id=rendered.uid, etag='"stale"')
    with pytest.raises(TargetError, match="etag conflict"):
        CalDavTarget("http://dav.local", transport=t).upsert(rendered, previous=prev)


def test_caldav_collection_change_deletes_the_old_resource(rendered):
    t = FakeTransport(FakeResponse(204), FakeResponse(201, {"ETag": '"v1"'}))
    prev = TargetRef(collection="practices", remote_id=rendered.uid, etag='"v1"')
    CalDavTarget("http://dav.local", transport=t).upsert(rendered, previous=prev)
    assert t.calls[0][0] == "DELETE"
    assert "/practices/" in t.calls[0][1]
    assert t.calls[1][0] == "PUT"
    assert "/games/" in t.calls[1][1]


# --- google -----------------------------------------------------------------


def test_google_event_ids_are_legal_and_stable():
    """Google requires base32hex (a-v, 0-9); our UIDs are not valid ids."""
    raw = "360Player-event-4823901"
    gid = google_event_id(raw)
    assert re.fullmatch(r"[0-9a-v]{5,1024}", gid), gid
    assert gid == google_event_id(raw)          # stable, or every poll duplicates
    assert gid != google_event_id(raw + "x")


def test_google_payload_shape(rendered):
    payload = to_google_event(rendered)
    assert payload["summary"] == "Jesse ⚽️ vs Harbour FC"
    assert payload["start"]["timeZone"] == "America/New_York"
    assert payload["status"] == "confirmed"
    assert payload["reminders"]["overrides"] == [{"method": "popup", "minutes": 90}]
    assert payload["location"].startswith("Thistledown Park")


def test_google_keeps_our_uid_in_extended_properties(rendered):
    private = to_google_event(rendered)["extendedProperties"]["private"]
    # The derived id is lossy, so this is the only way back to our event.
    assert private["calsync_uid"] == "360Player-event-4823901"
    assert private["calsync_source"] == "p360-jesse-vanguard"


def test_google_cancellation_status(rendered):
    assert to_google_event(replace(rendered, cancelled=True))["status"] == "cancelled"


def test_google_requires_an_explicit_calendar_mapping(rendered):
    target = GoogleCalendarTarget(calendar_map={"practices": "p@group.calendar.google.com"})
    # Guessing would put a kid's games in the wrong calendar; fail instead.
    with pytest.raises(TargetError, match="no Google calendar mapped"):
        target.upsert(rendered)


def test_google_upsert_targets_the_mapped_calendar(rendered):
    calls = []

    def transport(method, path, body=None):
        calls.append((method, path, body))
        return {}

    target = GoogleCalendarTarget(
        calendar_map={"games": "g@group.calendar.google.com"}, transport=transport
    )
    ref = target.upsert(rendered)
    assert calls[0][0] == "PUT"
    assert "g@group.calendar.google.com" in calls[0][1]
    assert ref.remote_id == google_event_id(rendered.uid)


def test_google_move_between_calendars_is_explicit(rendered):
    calls = []

    def transport(method, path, body=None):
        calls.append((method, path))
        return {}

    target = GoogleCalendarTarget(
        calendar_map={"games": "g@x", "practices": "p@x"}, transport=transport
    )
    prev = TargetRef(collection="practices", remote_id=google_event_id(rendered.uid))
    target.upsert(rendered, previous=prev)
    # An update against the new calendar would create a second copy.
    assert "/move?destination=g@x" in calls[0][1]
    assert calls[1][0] == "PUT"


def test_caldav_reads_an_etag_however_the_transport_cased_it(rendered):
    """urllib title-cases headers, so a real server's `ETag` arrives as `Etag`.

    Looking it up by exact spelling dropped every ETag: remote_etag stayed NULL,
    later writes fell back to `If-None-Match: *`, and every update to an event
    that already existed failed 412 — permanently, against any real server. The
    fakes here previously used the one spelling the target asked for, which is
    exactly why nothing caught it.
    """
    for spelling in ("ETag", "Etag", "etag", "ETAG"):
        target = CalDavTarget(
            base_url="http://dav.example/calsync",
            transport=FakeTransport(FakeResponse(201, {spelling: '"v1"'})),
        )
        assert target.upsert(rendered).etag == '"v1"', f"dropped {spelling}"
