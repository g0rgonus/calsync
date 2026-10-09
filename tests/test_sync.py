"""The sync loop closes.

The assertion that matters here is the second poll: run the same feed twice and
nothing should be written the second time. Before state persistence existed,
every poll re-created the whole season, which is invisible in unit tests of the
diff and obvious the moment the loop runs end to end.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from calsync import db, repo
from calsync.diff import diff_poll, fingerprint
from calsync.fetch import FetchError, render_url
from calsync.identity import IdentityError, extract, synthesize
from calsync.models import Event
from calsync.secrets import SecretError, SecretStore
from calsync.sync import sync_source
from calsync.targets import build

FIXTURE = Path(__file__).parent / "fixtures" / "player360_sample.ics"

#: The sample feed's events sit in mid-2026; anchor "now" just before them so
#: they land inside the sync window.
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "calsync.db")
    connection.executescript(
        """
        INSERT INTO children (id, name, initial, birth_order)
             VALUES ('jesse', 'Jesse', 'J', 1);
        INSERT INTO activities (id, child_id, name, sport_id, official_name,
                                league, age_group, tz, alarm_game_min, alarm_practice_min)
             VALUES ('jesse-soccer-vanguard', 'jesse', 'Vanguard', 'soccer', 'U10PL',
                     'PSL', 'U10', 'America/New_York', 90, 30);
        INSERT INTO sources (id, activity_id, kind, shape)
             VALUES ('p360-jesse-vanguard', 'jesse-soccer-vanguard', 'player360', 'feed');
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def source(conn):
    return repo.list_sources(conn)[0]


@pytest.fixture
def target(tmp_path):
    return build("ics_file", directory=tmp_path / "out")


def _sync(conn, source, target, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("raw", FIXTURE.read_bytes())
    return sync_source(conn, source, target, **kwargs)


# --- the loop closes --------------------------------------------------------


def test_first_poll_creates_and_writes_files(conn, source, target, tmp_path):
    report = _sync(conn, source, target)

    assert report.status == "ok"
    assert report.created > 0
    assert report.updated == 0
    written = list((tmp_path / "out").rglob("*.ics"))
    assert len(written) == report.created


def test_second_poll_of_the_same_feed_changes_nothing(conn, source, target):
    first = _sync(conn, source, target)
    second = _sync(conn, source, target)

    assert second.created == 0, "re-created events that were already synced"
    assert second.updated == 0, "rewrote events whose content had not changed"
    assert second.cancelled == 0, "cancelled events that were still in the feed"
    assert second.unchanged == first.created


def test_state_is_recorded_for_every_written_event(conn, source, target):
    report = _sync(conn, source, target)

    states = repo.event_states(conn, source.id)
    assert len(states) == report.created
    for state in states.values():
        assert state.content_hash, "a state row without a hash can never match"
        assert state.remote_id, "without remote_id we cannot cancel or move it later"
        assert not state.cancelled


def test_poll_run_is_logged(conn, source, target):
    _sync(conn, source, target)

    runs = list(conn.execute("SELECT status, raw_sha256 FROM poll_runs"))
    assert [r["status"] for r in runs] == ["ok"]
    assert runs[0]["raw_sha256"], "raw hash not recorded, so a repeat fetch is unprovable"
    assert conn.execute(
        "SELECT last_success_at FROM sources WHERE id = 'p360-jesse-vanguard'"
    ).fetchone()["last_success_at"]


# --- failures never read as cancellation ------------------------------------


def test_unparseable_feed_is_an_error_not_a_wipe(conn, source, target):
    _sync(conn, source, target)
    before = repo.event_states(conn, source.id)

    report = _sync(conn, source, target, raw=b"this is not a calendar")

    assert report.status == "error"
    assert report.cancelled == 0
    assert repo.event_states(conn, source.id) == before, "state changed on a failed poll"
    assert conn.execute(
        "SELECT last_error FROM sources WHERE id = 'p360-jesse-vanguard'"
    ).fetchone()["last_error"]


def test_empty_feed_is_an_error_not_a_wipe(conn, source, target):
    _sync(conn, source, target)

    report = _sync(
        conn, source, target,
        raw=b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nEND:VCALENDAR\r\n",
    )

    assert report.status == "error"
    assert report.cancelled == 0


def test_missing_secret_is_an_error_not_a_wipe(conn, source, target):
    conn.execute(
        "UPDATE sources SET url_template = 'https://x/e.ics?token={{secret:nope}}' "
        "WHERE id = 'p360-jesse-vanguard'"
    )
    conn.commit()

    report = sync_source(
        conn, repo.list_sources(conn)[0], target, now=NOW,
        secrets=SecretStore(path="/nonexistent/secrets.json"),
    )

    assert report.status == "error"
    assert report.cancelled == 0


# --- guards -----------------------------------------------------------------


def test_mass_disappearance_holds_cancellations(conn, source, target):
    _sync(conn, source, target)

    # A feed carrying only its first event: everything else "vanished".
    report = _sync(conn, source, target, raw=_first_event_only())

    assert report.status == "held"
    assert report.held_kind == "disappearance"
    assert report.cancelled == 0
    assert not any(s.cancelled for s in repo.event_states(conn, source.id).values())
    assert [r["status"] for r in conn.execute(
        "SELECT status FROM poll_runs ORDER BY id")] == ["ok", "held"]


def test_a_held_poll_names_what_it_held(conn, source, target):
    """"Pending confirmation" has to say pending confirmation *of what*."""
    _sync(conn, source, target)
    report = _sync(conn, source, target, raw=_first_event_only())

    assert len(report.held_cancellations) == 4
    assert report.held_fingerprint == fingerprint(report.held_cancellations)


def test_a_confirmed_hold_cancels_exactly_what_was_held(conn, source, target, tmp_path):
    """The way out of a guard that would otherwise hold until the season ends.

    A schedule rebuilt in the team's app under fresh ids trips the guard on
    every poll until the old events age out of the window — weeks of every
    practice on the calendar twice, and every genuine cancellation stuck behind
    them. A person who has looked at the list can say so.
    """
    _sync(conn, source, target)
    held = _sync(conn, source, target, raw=_first_event_only(), dry_run=True)

    report = _sync(conn, source, target, raw=_first_event_only(),
                   confirm_held=held.held_fingerprint)

    assert report.status == "ok"
    assert report.confirmed == report.cancelled == 4
    states = repo.event_states(conn, source.id)
    assert {uid for uid, s in states.items() if s.cancelled} == set(held.held_cancellations)
    assert len(list((tmp_path / "out").rglob("*.ics"))) == 1
    detail = conn.execute(
        "SELECT status, detail FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert detail["status"] == "ok"
    assert "confirmed by a person" in detail["detail"]


def test_a_confirmation_of_a_different_set_cancels_nothing(conn, source, target):
    """The feed is fetched again between the page and the button. Agreeing that
    four events are gone is not agreeing that whatever is missing by then is."""
    _sync(conn, source, target)
    shown = _sync(conn, source, target, raw=_first_event_only(), dry_run=True)
    stale = fingerprint(shown.held_cancellations[1:])

    report = _sync(conn, source, target, raw=_first_event_only(), confirm_held=stale)

    assert report.status == "held"
    assert report.confirmed == report.cancelled == 0
    assert not any(s.cancelled for s in repo.event_states(conn, source.id).values())


def test_a_dry_run_never_applies_a_confirmation(conn, source, target):
    _sync(conn, source, target)
    shown = _sync(conn, source, target, raw=_first_event_only(), dry_run=True)

    report = _sync(conn, source, target, raw=_first_event_only(), dry_run=True,
                   confirm_held=shown.held_fingerprint)

    assert report.status == "held"
    assert not any(s.cancelled for s in repo.event_states(conn, source.id).values())


def test_an_identity_break_cannot_be_confirmed():
    """It withholds creations as well, so "confirming" it would duplicate a
    season. There is no held set to fingerprint, and a forged one is refused."""
    delta = diff_poll(
        [_event("new-1", hash_="a"), _event("new-2", hash_="b")],
        {"old-1": "a", "old-2": "b"},
        now=NOW,
    )
    assert delta.anomaly_kind == "identity"
    assert delta.held_cancellations == []
    assert not delta.confirm(fingerprint([]))
    assert not delta.confirm(fingerprint(["old-1", "old-2"]))
    assert delta.is_anomalous


def test_a_confirmed_removal_is_on_record_before_any_delete(conn, source, target, tmp_path):
    """The Mac mirror reads Radicale and asks `/v1/placements` to account for
    what is missing. If the approval landed after the deletes, a mirror run in
    between would see an absence nothing explains, hold it, and ask a second
    person for the decision the first had just made."""
    _sync(conn, source, target)
    shown = _sync(conn, source, target, raw=_first_event_only(), dry_run=True)

    on_record = []
    real_cancel = target.cancel

    def cancel(ref):
        # A separate connection sees only what is committed — which is what
        # the API process would see at this moment.
        other = db.connect(tmp_path / "calsync.db")
        row = other.execute(
            "SELECT removal_approved_at FROM event_state WHERE uid = ?",
            (ref.remote_id,),
        ).fetchone()
        other.close()
        on_record.append(row[0])
        return real_cancel(ref)

    target.cancel = cancel
    _sync(conn, source, target, raw=_first_event_only(),
          confirm_held=shown.held_fingerprint)

    assert len(on_record) == 4
    assert all(on_record), "a delete went out before its approval was committed"


def test_an_event_written_again_is_no_longer_approved_for_removal(conn, source, target):
    """A stale approval would let a later bad read of a live event pass as
    deliberate on the mirror."""
    _sync(conn, source, target)
    uid = next(iter(repo.event_states(conn, source.id)))
    repo.approve_removals(conn, [uid])
    assert _placement(conn, uid)["state"] == "approved"

    conn.execute("UPDATE event_state SET content_hash = 'stale' WHERE uid = ?", (uid,))
    _sync(conn, source, target)

    assert _placement(conn, uid)["state"] == "live"


def test_placements_say_why_an_event_is_off(conn, source, target):
    _sync(conn, source, target)
    live, approved, cancelled, withheld = sorted(repo.event_states(conn, source.id))[:4]
    repo.approve_removals(conn, [approved])
    repo.mark_event_cancelled(conn, cancelled)
    repo.set_withheld(conn, withheld, "not_attending")
    repo.mark_event_cancelled(conn, withheld)

    assert _placement(conn, live)["state"] == "live"
    assert _placement(conn, approved)["state"] == "approved"
    assert _placement(conn, cancelled)["state"] == "cancelled"
    assert _placement(conn, withheld)["state"] == "withheld", "the reason, not the result"


def _placement(conn, uid):
    return next(p for p in repo.placements(conn, since="2000-01-01") if p["uid"] == uid)


def test_a_held_poll_is_listed_until_one_gets_through(conn, source, target):
    _sync(conn, source, target)
    assert repo.held_polls(conn) == []

    _sync(conn, source, target, raw=_first_event_only())
    assert [r["source_id"] for r in repo.held_polls(conn)] == [source.id]

    # A failed fetch says nothing about whether the hold is still there.
    _sync(conn, source, target, raw=b"not a calendar")
    assert [r["source_id"] for r in repo.held_polls(conn)] == [source.id]

    _sync(conn, source, target)
    assert repo.held_polls(conn) == []


def test_identity_break_holds_creations_too(conn, source, target, tmp_path):
    """The flag-football failure: every UID is new, so nothing matches.

    The disappearance guard alone would withhold the deletions and then happily
    write a duplicate of the entire season.
    """
    _sync(conn, source, target)
    before = len(list((tmp_path / "out").rglob("*.ics")))

    rewritten = FIXTURE.read_bytes().replace(b"360Player-event-", b"360Player-event-99")
    report = _sync(conn, source, target, raw=rewritten)

    assert report.status == "held"
    assert report.held_kind == "identity"
    assert report.created == 0, "duplicated the season under fresh UIDs"
    assert report.cancelled == 0
    assert len(list((tmp_path / "out").rglob("*.ics"))) == before


def test_dry_run_writes_nothing(conn, source, target, tmp_path):
    report = _sync(conn, source, target, dry_run=True)

    assert report.created > 0
    assert not list((tmp_path / "out").rglob("*.ics"))
    assert repo.event_states(conn, source.id) == {}
    assert not list(conn.execute("SELECT 1 FROM poll_runs"))


def test_events_outside_the_sync_window_are_skipped(conn, source, target):
    # Anchor "now" a decade on: every sample event falls behind the back window.
    report = _sync(conn, source, target, now=NOW + timedelta(days=3650))

    assert report.skipped_window > 0
    assert report.created == 0


def _first_event_only() -> bytes:
    """Rebuild the fixture carrying only its first VEVENT."""
    text = FIXTURE.read_text()
    head, _, rest = text.partition("BEGIN:VEVENT")
    first, _, _ = rest.partition("END:VEVENT")
    return (head + "BEGIN:VEVENT" + first + "END:VEVENT\r\nEND:VCALENDAR\r\n").encode()


# --- identity policy --------------------------------------------------------


def test_extract_pulls_the_stable_id_out_of_a_timestamped_uid():
    """The observed flag-football shape: <event_id><generation timestamp>."""
    first = "127823172026-04-24T17:47:04.859629"
    second = "127823172026-04-24T18:06:30.105264"

    assert extract(first, r"^(?P<id>\d{8})") == extract(second, r"^(?P<id>\d{8})")


def test_extract_raises_rather_than_falling_back_to_the_raw_uid():
    with pytest.raises(IdentityError):
        extract("no-digits-here", r"^(?P<id>\d{8})")


def test_synthesized_uid_is_stable_for_the_same_content():
    when = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
    a = synthesize(activity_id="x", starts_at=when, summary="U10PL  Practice")
    b = synthesize(activity_id="x", starts_at=when, summary="U10PL Practice")
    assert a == b
    assert a != synthesize(activity_id="y", starts_at=when, summary="U10PL Practice")


# --- url assembly -----------------------------------------------------------


def test_secrets_never_appear_in_the_redacted_url(tmp_path):
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"p360_token": "SUPERSECRET"}))
    secrets_file.chmod(0o600)
    store = SecretStore(path=secrets_file)

    assembled = render_url(
        "https://api.example.com/e.ics?token={{secret:p360_token}}&from={{now-30d|unix}}",
        secrets=store, now=NOW,
    )

    assert "SUPERSECRET" in assembled.url
    assert "SUPERSECRET" not in assembled.redacted
    assert "SUPERSECRET" not in str(assembled)
    assert "SUPERSECRET" not in repr(assembled)


def test_now_placeholder_moves_with_the_clock(tmp_path):
    store = SecretStore(path=tmp_path / "absent.json")
    template = "https://x/e.ics?from={{now-30d|unix}}"

    early = render_url(template, secrets=store, now=NOW)
    later = render_url(template, secrets=store, now=NOW + timedelta(days=1))

    assert early.url != later.url, "a frozen `from` drifts into the past"


def test_group_readable_secrets_file_is_refused(tmp_path):
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"p360_token": "x"}))
    secrets_file.chmod(0o644)

    with pytest.raises(SecretError, match="chmod 600"):
        SecretStore(path=secrets_file).get("p360_token")


def test_non_http_template_is_refused(tmp_path):
    store = SecretStore(path=tmp_path / "absent.json")
    with pytest.raises(FetchError):
        render_url("file:///etc/passwd", secrets=store, now=NOW)


# --- the diff guard in isolation --------------------------------------------


def _event(uid: str, *, hash_: str) -> Event:
    return Event(
        uid=uid, activity_id="a",
        starts_at=NOW, ends_at=NOW,
        is_game=False, tz="UTC", content_hash=hash_,
    )


def test_identity_guard_does_not_fire_on_a_normal_partial_change():
    known = {"a": "1", "b": "2", "c": "3"}
    incoming = [_event("a", hash_="1"), _event("b", hash_="CHANGED"), _event("d", hash_="4")]

    delta = diff_poll(incoming, known, now=NOW)

    assert delta.anomaly_kind != "identity"


def test_identity_guard_does_not_fire_on_a_first_ever_poll():
    delta = diff_poll([_event("a", hash_="1")], {}, now=NOW)

    assert not delta.is_anomalous
    assert len(delta.created) == 1


# --- venue enrichment must not lose information ------------------------------


def test_alias_resolution_enriches_but_never_downgrades(conn, source, target):
    """A name-only venue row must not erase an address the feed supplied.

    Seeding an activity's home_venue creates exactly such a stub, and replacing
    outright turned "Thistledown Park, 1009 Thistledown Rd" into a bare name.
    """
    conn.execute("INSERT INTO venues (canonical_name) VALUES ('Alder Reach Memorial Park')")
    conn.execute(
        "INSERT INTO venue_aliases (venue_id, alias, source) "
        "SELECT id, 'Alder Reach Memorial Park', 'test' FROM venues "
        "WHERE canonical_name = 'Alder Reach Memorial Park'"
    )
    conn.commit()

    _sync(conn, source, target)

    written = "\n".join(p.read_text() for p in target.directory.rglob("*.ics"))
    assert "7160 Kestrel Ln" in written, "alias lookup discarded the feed's address"


def test_alias_resolution_supplies_what_the_feed_lacks(conn, source, target):
    """The other direction: the table fills in an address the feed never had."""
    conn.execute(
        "INSERT INTO venues (canonical_name, address, lat, lon, pin_confirmed) "
        "VALUES ('Alder Reach Memorial Park', '7160 Kestrel Ln, Fenwick, NX 40219', "
        "37.5, -75.8, 1)"
    )
    conn.execute(
        "INSERT INTO venue_aliases (venue_id, alias, source) "
        "SELECT id, 'Alder Reach Memorial Park', 'test' FROM venues "
        "WHERE canonical_name = 'Alder Reach Memorial Park'"
    )
    conn.commit()

    _sync(conn, source, target)

    written = "\n".join(p.read_text() for p in target.directory.rglob("*.ics"))
    # The address, which is the whole point of the table filling one in — and
    # now the whole of what a maps app needs, since calsync stopped emitting a
    # coordinate pin.
    assert "7160 Kestrel Ln" in written, "the known address never reached the event"


# --- diagnostics reach the report -------------------------------------------


def test_adapter_diagnostics_reach_the_report(conn, source, target):
    """These were computed and dropped; the promotion gate depends on them."""
    ics = FIXTURE.read_text().replace("CATEGORIES:practice", "CATEGORIES:team photo", 1)

    report = _sync(conn, source, target, raw=ics.encode())

    assert report.diagnostics.get("unknown_categories") == ["team photo"]
    assert not report.is_clean
    assert any("unrecognised categories" in line for line in report.diagnostic_lines())


def test_unresolved_venues_are_reported(conn, source, target):
    """Only the sync layer can see this — the adapter has no database."""
    report = _sync(conn, source, target)

    assert report.diagnostics.get("unresolved_venues"), "no venue rows exist, so all are unresolved"
    assert not report.is_clean


def test_a_resolved_venue_clears_that_diagnostic(conn, source, target):
    for name in ("Alder Reach Memorial Park", "Thistledown Park", "Kiln Creek Park"):
        conn.execute(
            "INSERT INTO venues (canonical_name, address) VALUES (?, '1 Somewhere Rd')",
            (name,),
        )
        conn.execute(
            "INSERT INTO venue_aliases (venue_id, alias, source) "
            "SELECT id, ?, 'test' FROM venues WHERE canonical_name = ?",
            (name, name),
        )
    conn.commit()

    report = _sync(conn, source, target)

    assert not report.diagnostics.get("unresolved_venues")


def test_fixtures_seen_counts_games(conn, source, target):
    report = _sync(conn, source, target)
    assert report.fixtures_seen > 0


# --- staging and promotion ---------------------------------------------------


def _stage(conn, collection="onboarding"):
    repo.set_staging(conn, "p360-jesse-vanguard", collection)
    return repo.list_sources(conn)[0]


def test_staged_source_writes_to_one_collection(conn, target, tmp_path):
    staged = _stage(conn)

    report = _sync(conn, staged, target)

    assert report.staged_to == "onboarding"
    written = list((tmp_path / "out").rglob("*.ics"))
    assert written, "nothing written"
    assert {p.parent.name for p in written} == {"onboarding"}, "staging did not override routing"


def test_promotion_moves_events_rather_than_duplicating(conn, target, tmp_path):
    """A collection change is a move, so the staged copies must not survive."""
    staged = _stage(conn)
    _sync(conn, staged, target)
    staged_count = len(list((tmp_path / "out" / "onboarding").rglob("*.ics")))
    assert staged_count > 0

    repo.set_staging(conn, "p360-jesse-vanguard", None)
    promoted = repo.list_sources(conn)[0]
    _sync(conn, promoted, target)

    assert not list((tmp_path / "out" / "onboarding").rglob("*.ics")), "left ghosts in staging"
    live = list((tmp_path / "out").rglob("*.ics"))
    assert len(live) == staged_count, "duplicated events instead of moving them"
    assert {p.parent.name for p in live} <= {"games", "practices"}


def test_promotable_requires_a_clean_parse(conn, source, target):
    dirty = _sync(conn, source, target, dry_run=True)
    assert not dirty.promotable, "promotable despite unresolved venues"


def test_promotable_requires_a_fixture_to_have_been_seen(conn, source, target):
    """A practices-only feed has never exercised the opponent path."""
    report = _sync(conn, source, target, dry_run=True)
    report.diagnostics = {}
    report.fixtures_seen = 0

    assert not report.promotable

    report.fixtures_seen = 1
    assert report.promotable


def test_events_ageing_out_of_the_window_are_not_a_mass_cancellation(conn, source, target):
    """The failure a live container surfaced: a finished season ages past the
    back window, every tracked event looks missing at once, and the
    disappearance guard fires on what is really just the windmere of time."""
    _sync(conn, source, target)
    assert repo.event_states(conn, source.id)

    # A year later, with the same feed still serving the same (now old) events.
    report = _sync(conn, source, target, now=NOW + timedelta(days=365))

    assert report.status == "ok", f"guard tripped on ageing, not cancellation: {report.held}"
    assert report.cancelled == 0
    assert report.skipped_window > 0


# --- an edit the feed does not explain ---------------------------------------
#
# Observed on 2026-08-20: a practice cancelled in the app was still exported as
# an ordinary practice, with only LAST-MODIFIED moved. The app knew; the feed
# never said. docs/sources/player360.md, Trap 2.

EDIT_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _p360(modified, *, start="20260820T214500Z", end="20260820T230000Z"):
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//360Player//EN",
        "BEGIN:VEVENT", "UID:e1", "SUMMARY:U10DA Practice",
        f"DTSTART:{start}", f"DTEND:{end}",
        "LOCATION:Kiln Creek Park\\, 2901 Kiln Creek Pkwy\\, Yorktown VA",
        "CATEGORIES:practice", f"LAST-MODIFIED:{modified}",
        "END:VEVENT", "END:VCALENDAR", "",
    ]).encode()


def test_an_edit_the_feed_does_not_explain_is_reported(conn, source, target):
    """The whole signal: content identical, LAST-MODIFIED moved to before the
    event. That is the only trace a cancellation leaves."""
    report = sync_source(conn, source, target, now=EDIT_NOW,
                         raw=_p360("20260812T214557Z"))
    assert report.created == 1
    assert report.edited_upstream == [], "nothing to compare against yet"

    # Cancelled in the app two hours before it starts. Every published field is
    # unchanged.
    report = sync_source(conn, source, target, now=EDIT_NOW,
                         raw=_p360("20260820T195521Z"))
    assert report.unchanged == 1, "the content really is identical"
    assert report.edited_upstream == ["e1"]
    assert "changed upstream unseen" in report.line()

    row = conn.execute(
        "SELECT upstream_edit_at, cancelled FROM event_state WHERE uid = 'e1'"
    ).fetchone()
    assert row["upstream_edit_at"] is not None
    assert row["cancelled"] == 0, "a timestamp is not grounds for a delete"


def test_the_churn_after_an_event_ends_is_not_an_edit(conn, source, target):
    """Player360 bumps LAST-MODIFIED 2-5s after every DTEND as a matter of
    course. Without the before-the-event rule this fires on every event on the
    evening it happened — the noise `content_hash` excludes the field to avoid.
    """
    sync_source(conn, source, target, now=EDIT_NOW, raw=_p360("20260812T214557Z"))
    report = sync_source(conn, source, target, now=EDIT_NOW,
                         raw=_p360("20260820T230002Z"))     # 2s after DTEND

    assert report.edited_upstream == []
    row = conn.execute(
        "SELECT upstream_edit_at, upstream_modified_at FROM event_state WHERE uid = 'e1'"
    ).fetchone()
    assert row["upstream_edit_at"] is None, "churn raised a flag"
    assert row["upstream_modified_at"] is not None, "but it was still recorded"


def test_an_edit_that_changes_something_we_read_is_not_unexplained(
    conn, source, target
):
    """A moved event is an ordinary update. The notice is only for edits whose
    substance the feed withholds."""
    sync_source(conn, source, target, now=EDIT_NOW, raw=_p360("20260812T214557Z"))
    report = sync_source(conn, source, target, now=EDIT_NOW,
                         raw=_p360("20260820T195521Z", start="20260820T203000Z",
                                   end="20260820T220000Z"))

    assert report.updated == 1
    assert report.edited_upstream == [], "the feed said what changed"


def test_an_unexplained_edit_does_not_rewrite_the_event(conn, source, target, tmp_path):
    """The invariant this must not break: `content_hash` is the authority and
    never keys off LAST-MODIFIED, or every event re-pushes as it ends."""
    sync_source(conn, source, target, now=EDIT_NOW, raw=_p360("20260812T214557Z"))
    before = conn.execute(
        "SELECT content_hash FROM event_state WHERE uid = 'e1'").fetchone()[0]
    written = sorted((tmp_path / "out").rglob("*.ics"))
    stamps = {p: p.stat().st_mtime_ns for p in written}

    report = sync_source(conn, source, target, now=EDIT_NOW,
                         raw=_p360("20260820T195521Z"))

    after = conn.execute(
        "SELECT content_hash FROM event_state WHERE uid = 'e1'").fetchone()[0]
    assert before == after
    assert report.created == report.updated == report.refreshed == 0
    assert {p: p.stat().st_mtime_ns for p in written} == stamps, "the event was rewritten"


# --- the feed says cancelled ------------------------------------------------
# Player360 now exports STATUS:CANCELLED (docs/sources/player360.md, Trap 2).
# It is honoured outright: no guard, no person, however many at once.


def _called_off(*uids: str, raw: bytes | None = None) -> bytes:
    text = (raw or FIXTURE.read_bytes()).decode()
    for uid in uids:
        line = next(l for l in text.splitlines(keepends=True) if l.rstrip() == f"UID:{uid}")
        text = text.replace(line, line + "STATUS:CANCELLED\r\n")
    return text.encode()


def _all_uids() -> list[str]:
    return [l.rstrip()[4:] for l in FIXTURE.read_text().splitlines() if l.startswith("UID:")]


def test_every_event_called_off_at_once_is_cancelled_without_a_hold(
    conn, source, target, tmp_path
):
    """The whole feed at once: far past any guard, and still not a fault. The
    feed parsed and named each event — nothing a broken fetch produces."""
    _sync(conn, source, target)
    uids = _all_uids()

    report = _sync(conn, source, target, raw=_called_off(*uids))

    assert report.status == "ok"
    assert report.held is None
    assert report.cancelled == report.called_off == len(uids)
    assert all(s.cancelled for s in repo.event_states(conn, source.id).values())
    assert list((tmp_path / "out").rglob("*.ics")) == []
    assert repo.held_polls(conn) == []
    assert "by the feed" in conn.execute(
        "SELECT detail FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_a_called_off_event_is_never_written(conn, source, target):
    uid = _all_uids()[0]
    report = _sync(conn, source, target, raw=_called_off(uid))

    assert report.created == len(_all_uids()) - 1
    assert uid not in repo.event_states(conn, source.id)
    again = _sync(conn, source, target, raw=_called_off(uid))
    assert again.created == again.cancelled == 0


def test_a_feed_cancellation_is_on_record_before_its_delete(conn, source, target, tmp_path):
    """The mirror reads Radicale, then asks `/v1/placements`. A delete landing
    before calsync could account for it would be an absence nothing explains —
    ten of them and the mirror holds, asking a person about a rained-out
    weekend the feed already announced."""
    _sync(conn, source, target)
    uids = _all_uids()

    seen = []
    real_cancel = target.cancel

    def cancel(ref):
        other = db.connect(tmp_path / "calsync.db")
        seen.append(next(p["state"] for p in repo.placements(other, since="2000-01-01")
                         if p["uid"] == ref.remote_id))
        other.close()
        return real_cancel(ref)

    target.cancel = cancel
    _sync(conn, source, target, raw=_called_off(*uids))

    assert seen == ["cancelled"] * len(uids)


def test_a_failed_delete_of_a_called_off_event_is_retried(conn, source, target):
    """Recorded first, but not tombstoned until the target agrees — a row
    marked cancelled drops out of the diff, and the event would stay on the
    calendar with nothing left to remove it."""
    from calsync.targets import TargetError

    _sync(conn, source, target)
    uid = _all_uids()[0]
    real_cancel = target.cancel

    def refuse(ref):
        raise TargetError("server went away")

    target.cancel = refuse
    failed = _sync(conn, source, target, raw=_called_off(uid))
    assert failed.status == "error"
    assert not repo.event_states(conn, source.id)[uid].cancelled
    assert _placement(conn, uid)["state"] == "cancelled", "the mirror is still told"

    target.cancel = real_cancel
    retried = _sync(conn, source, target, raw=_called_off(uid))
    assert retried.status == "ok"
    assert retried.called_off == 1
    assert repo.event_states(conn, source.id)[uid].cancelled


def test_a_game_the_feed_reinstates_comes_back(conn, source, target, tmp_path):
    _sync(conn, source, target)
    uid = _all_uids()[0]
    _sync(conn, source, target, raw=_called_off(uid))

    report = _sync(conn, source, target)

    assert report.created == 1
    assert not repo.event_states(conn, source.id)[uid].cancelled
    assert _placement(conn, uid)["state"] == "live"


def test_a_called_off_game_takes_its_warm_up_with_it(conn, source, target):
    conn.execute("UPDATE activities SET warmup_minutes = 45")
    conn.commit()
    _sync(conn, source, target)
    states = repo.event_states(conn, source.id)
    game = next(uid for uid in states if f"calsync-warmup-{uid}" in states)

    report = _sync(conn, source, target, raw=_called_off(game))

    assert report.called_off == 2
    states = repo.event_states(conn, source.id)
    assert states[game].cancelled
    assert states[f"calsync-warmup-{game}"].cancelled


def test_feed_cancellations_go_through_while_a_disappearance_is_held(
    conn, source, target
):
    """A held absence is a question about the fetch. A game the feed called off
    in that same fetch is not part of the question."""
    _sync(conn, source, target)
    text = _first_event_only()
    first = _all_uids()[0]

    report = _sync(conn, source, target, raw=_called_off(first, raw=text))

    assert report.status == "held"
    assert report.called_off == 1
    assert first not in report.held_cancellations
    assert repo.event_states(conn, source.id)[first].cancelled


def test_the_poll_log_counts_what_was_written(conn, source, target):
    """A withheld event the feed still publishes is "new" to the diff on every
    poll. The log recorded that for weeks while nothing was written."""
    _sync(conn, source, target)
    uid = _all_uids()[1]
    repo.set_withheld(conn, uid, "not_attending")
    repo.mark_event_cancelled(conn, uid)
    conn.commit()

    report = _sync(conn, source, target)

    assert report.created == 0
    detail = conn.execute(
        "SELECT detail FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert detail.startswith("0 new"), detail
    assert "1 withheld" in detail
