"""The sync loop: fetch, diff, write, record.

This is the only place that closes the loop. Everything else in the package is
a pure function or a single-purpose reader, which is what makes them testable;
this module is where the ordering rules live, and the ordering is the safety:

1. **A failed fetch or parse aborts before the diff.** Zero events and "every
   event cancelled" are the same shape, so an error must never reach
   :func:`~calsync.diff.diff_poll`.
2. **State is recorded only after the target accepts the write.** The reverse
   order makes a failed write look synced, and the event is never retried.
3. **A held diff writes a ``poll_runs`` row and nothing else.** A guard trip
   that leaves no trace is a guard nobody acts on.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from . import correlate, overlay, repo, retire, sources, warmup, withheld
from .diff import diff_poll, fingerprint
from .fetch import FetchError, http_fetch, render_url
from .models import Activity, Event, Venue
from .render import render
from .routing import collection_for
from .secrets import SecretError, SecretStore
from .settings import Settings
from .sources import SourceError
from .targets import TargetError, TargetRef


@dataclass
class SyncReport:
    source_id: str
    status: str = "ok"                     # ok | held | error
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    cancelled: int = 0
    skipped_window: int = 0
    #: Games seen in this poll. Zero means the fixture path is unproven — a
    #: coach publishes practices first and adds the schedule later.
    fixtures_seen: int = 0
    #: The source is pushed to rather than fetched (`sources.PUSHED`). Its
    #: sender states each event's type outright, so there is no fixture parsing
    #: to prove; what the gate waits for instead is the first push.
    pushed: bool = False
    #: Events in this poll that are on, in the window. For a pushed source this
    #: is the gate's evidence, where a feed's is `fixtures_seen`.
    events_seen: int = 0
    #: Events relocated because their collection changed, not their content —
    #: promotion off the onboarding calendar, or a routing template change.
    moved: int = 0
    #: Events re-written because what we had stored for them no longer matched
    #: what the feed derives to, though the feed itself had not changed — a venue
    #: gaining an address, an alias being taught, or a row predating
    #: `event_content` entirely. Counted apart from `updated` because `updated`
    #: means the feed changed and these are precisely the ones where it did not.
    refreshed: int = 0
    #: Events re-written because a person's override of them changed — set,
    #: edited or lifted — while the feed did not (`overlay.py`). Its own count
    #: for `refreshed`'s reason: neither the feed nor the venue table moved.
    amended: int = 0
    #: A person's fixes this poll found the feed has caught up with, and lifted
    #: — the calendar did not change, because the two layers now agree.
    fixes_cleared: int = 0
    #: Fixes the feed now contradicts with a value of its own, waiting for a
    #: person to say which is right. The fix stays on the calendar meanwhile.
    fixes_changed: int = 0
    #: Fixes whose event has gone from the feed, waiting for a person to say
    #: where they belong.
    fixes_orphaned: int = 0
    #: Of `cancelled`, how many the feed itself marked ``STATUS:CANCELLED``
    #: rather than dropped. Applied without a guard or a person, however many.
    called_off: int = 0
    #: Events a person took off the calendar — not attending, or cancelled in a
    #: way the feed never says (`withheld.py`). Counted every poll because the
    #: feed goes on publishing them and the decision goes on being applied;
    #: reported so a season of standing exclusions is visible rather than
    #: showing up as a permanent shortfall in `created`.
    withheld: int = 0
    #: Events held off the real calendar because calsync could not tell which
    #: one they belong in. Not an error and not a guard trip — a question
    #: waiting for an answer, counted so the poller says so out loud.
    awaiting_review: int = 0
    held: str | None = None
    held_kind: str | None = None
    #: The uids a tripped disappearance guard withheld, and the fingerprint a
    #: person confirms them by (`diff.Diff.confirm`). Empty unless held.
    held_cancellations: list[str] = field(default_factory=list)
    held_fingerprint: str | None = None
    #: Cancellations applied because a person confirmed a held set.
    confirmed: int = 0
    errors: list[str] = field(default_factory=list)
    #: Other sources synced because this one's games changed what they share
    #: with it (`correlate.py`); their errors are in `errors`.
    partners_synced: list[str] = field(default_factory=list)

    #: Which collection the events actually went to, so a staged run says so.
    staged_to: str | None = None

    #: Everything the parse could not account for — the adapter's own gaps plus
    #: venues that matched no row. This is the promotion gate
    #: (docs/ONBOARDING.md §5), and until it is empty a source stays staged.
    diagnostics: dict[str, list[str]] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        return not any(self.diagnostics.values())

    @property
    def promotable(self) -> bool:
        """Safe to move off the onboarding calendar?

        Requires a fixture to have been seen: a feed carrying only practices has
        not exercised the opponent path at all, so a clean parse proves nothing
        about it yet (docs/ONBOARDING.md §5).
        """
        return (
            self.status == "ok"
            and self.is_clean
            and not self.errors
            and (self.events_seen > 0 if self.pushed else self.fixtures_seen > 0)
        )

    def tally(self) -> str:
        """What this poll did to the calendar, which is what `poll_runs` keeps."""
        parts = [
            f"{self.created} new",
            f"{self.updated} changed",
            f"{self.unchanged} unchanged",
            f"{self.cancelled} cancelled",
        ]
        if self.called_off:
            parts.append(f"{self.called_off} of them by the feed")
        if self.moved:
            parts.append(f"{self.moved} moved")
        if self.refreshed:
            parts.append(f"{self.refreshed} refreshed")
        if self.amended:
            parts.append(f"{self.amended} amended")
        if self.withheld:
            parts.append(f"{self.withheld} withheld")
        if self.fixes_cleared:
            parts.append(f"{self.fixes_cleared} override(s) cleared, the feed agrees")
        if self.fixes_changed:
            parts.append(f"{self.fixes_changed} override(s) the feed now disputes")
        if self.fixes_orphaned:
            parts.append(f"{self.fixes_orphaned} override(s) orphaned")
        return ", ".join(parts)

    def line(self) -> str:
        parts = [f"{self.source_id}: {self.status}", self.tally()]
        if self.awaiting_review:
            parts.append(f"{self.awaiting_review} awaiting review")
        if self.skipped_window:
            parts.append(f"{self.skipped_window} outside window")
        if self.staged_to:
            parts.append(f"staged to {self.staged_to!r}")
        if self.confirmed:
            parts.append(f"{self.confirmed} held cancellation(s) confirmed")
        if self.partners_synced:
            parts.append(f"also synced {', '.join(self.partners_synced)}")
        if self.held:
            parts.append(f"HELD ({self.held_kind}): {self.held}")
        parts.extend(f"ERROR: {e}" for e in self.errors)
        return ", ".join(parts)

    def diagnostic_lines(self) -> list[str]:
        """Human-readable parse gaps, each one an action for somebody."""
        labels = {
            "unknown_types": "unrecognised event types",
            "unknown_categories": "unrecognised categories",
            "unidentified": "fixtures where our team was not recognised",
            "unresolved_venues": "venues not yet in the venue table",
        }
        return [
            f"{labels.get(kind, kind)}: {', '.join(values)}"
            for kind, values in sorted(self.diagnostics.items())
            if values
        ]


def _in_window(event: Event, *, now: datetime, settings: Settings) -> bool:
    """Keep the calendar to a useful span.

    Past events age out of feeds normally; backfilling years of them into a
    calendar the family actually reads is noise, not history.
    """
    earliest = now - timedelta(days=settings.sync_window_back_days)
    latest = now + timedelta(days=settings.sync_window_forward_days)
    return earliest <= event.starts_at <= latest


def _enrich_venue(conn, event: Event) -> bool:
    """Upgrade a parsed venue to a known one, in place. True if a row matched.

    The adapter can only split a free-text string; coordinates come from the
    venue tables. Checked by raw string first, then by parsed name, because the
    alias table records exactly the strings seen in the wild.
    """
    if event.venue is None:
        return True  # nothing to resolve is not an unresolved venue
    for candidate in (event.venue.raw, event.venue.name):
        if not candidate:
            continue
        resolved = repo.resolve_venue_alias(conn, candidate)
        if resolved is not None:
            # Merge, never replace. A venue row can exist with only a name —
            # seeding an activity's home_venue creates exactly that — and
            # overwriting would throw away an address the feed did supply.
            # The table canonicalizes and enriches; it never destroys.
            event.venue = Venue(
                raw=event.venue.raw,
                name=resolved.name or event.venue.name,
                address=resolved.address or event.venue.address,
                lat=resolved.lat if resolved.lat is not None else event.venue.lat,
                lon=resolved.lon if resolved.lon is not None else event.venue.lon,
                pin_confirmed=resolved.pin_confirmed,
                # The table knows the place, not which field within it.
                field=event.venue.field,
            )
            return True
    return False


def _guard_thresholds(source: repo.Source, settings: Settings) -> tuple[float, int]:
    """Per-source overrides, falling back to instance settings.

    Config may express the percentage as either 20 or 0.20; normalize, because
    a 20.0 read as a fraction would disable the guard entirely.
    """
    guards = (source.config or {}).get("guards") or {}
    pct = guards.get("max_disappearance_pct", settings.max_disappearance_pct)
    count = guards.get("max_disappearance_count", settings.max_disappearance_count)
    pct = float(pct)
    if pct > 1:
        pct = pct / 100.0
    return pct, int(count)


def _pushed(conn, source: repo.Source) -> bytes:
    """An inbound source's store, read where a feed would be fetched.

    Items are never removed from it, so an empty store beside events this
    source has already written means the store was damaged — and handing the
    diff zero events would read as every one of them cancelled. Refused here
    for the same reason an empty feed is.
    """
    payload = repo.inbound_payload(conn, source.id)
    if not repo.inbound_items(conn, source.id) and repo.tracked_events(conn, source.id):
        raise FetchError(
            f"inbound source {source.id} has events on the calendar but nothing "
            "stored; refusing to read that as every one of them cancelled"
        )
    return payload


def sync_source(
    conn,
    source: repo.Source,
    target,
    *,
    now: datetime,
    secrets: SecretStore | None = None,
    fetcher=http_fetch,
    raw: bytes | str | None = None,
    dry_run: bool = False,
    confirm_held: str | None = None,
    follow: bool = True,
) -> SyncReport:
    """Poll one source and reconcile it into ``target``.

    ``raw`` bypasses the network with a payload already in hand — how the golden
    tests run, and how a saved feed can be replayed without a credential.

    ``confirm_held`` is a person's answer to a held disappearance: the
    fingerprint of the set they were shown. If this poll's held set is exactly
    that set, those cancellations go through like any other; if the feed has
    moved since, the poll stays held and nothing is cancelled. Only a person
    reaches this — the console's confirm button — never the poller.

    ``follow`` syncs the other sources that share a meet with this one once its
    games change; those runs pass False, so it goes one hop and stops.
    """
    report = SyncReport(
        source_id=source.id, staged_to=source.staging_collection,
        pushed=source.kind in sources.PUSHED,
    )
    settings = Settings.load(conn)
    activity: Activity = repo.get_activity(conn, source.activity_id)
    # Every kid on the team; one of them missing an event takes them off that
    # event's title, and the event goes only when nobody is left (`withheld`).
    children = repo.team_children(conn, activity)
    away = repo.absences(conn, source.id)

    merged_kids: dict[str, list] = {}   # carrier uid -> every team's kids going

    def kids_for(uid: str) -> list:
        base = warmup.parent_of(uid) or uid
        if base in merged_kids:
            return merged_kids[base]
        gone = away.get(base, set())
        return [c for c in children if c.id not in gone] or children

    # --- fetch + parse: any failure aborts before the diff ------------------
    try:
        if raw is None and report.pushed:
            raw = _pushed(conn, source)
        if raw is None:
            if not source.url_template:
                raise FetchError(f"source {source.id} has no url_template and no payload")
            assembled = render_url(
                source.url_template, secrets=secrets or SecretStore(), now=now
            )
            raw = fetcher(assembled)
        result = sources.parse(
            source.kind, raw, activity, source_id=source.id, config=source.config
        )
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad. FetchError/SecretError/SourceError are the
        # expected failures, but an adapter raising something unforeseen must
        # land here too — propagating would abort the run for every other
        # source, and swallowing it upstream could look like an empty feed.
        report.status = "error"
        report.errors.append(str(exc))
        repo.record_poll_run(conn, source_id=source.id, status="error", detail=str(exc))
        repo.record_source_error(conn, source.id, str(exc))
        conn.commit()
        return report

    # Carry the adapter's own parse gaps into the report; they are the promotion
    # gate, and until now they were computed and dropped.
    report.diagnostics = {k: list(v) for k, v in result.diagnostics.items()}

    # Loaded before anything is counted, because a person's standing decision to
    # keep an event off the calendar changes what a poll reports as much as what
    # it writes — and the console's gate is a preview. A dry run that counted a
    # withheld game as "1 new" would be reporting a write that is never going to
    # happen, and a venue warning for it would be asking a question about a
    # place nobody is going.
    states = repo.event_states(conn, source.id)
    off = withheld.off_uids(conn, source.id, states)
    # A person's corrections, laid over the feed below. Loaded here for the same
    # reason as `off`: a venue somebody has already corrected is not a question
    # worth asking about the place the feed named.
    overrides = repo.overrides_for(conn, source.id)

    events = []
    called_off: set[str] = set()
    unresolved_venues: set[str] = set()
    placed: set[str] = set()   # uids whose venue is one we know
    for event in result.events:
        if not _in_window(event, now=now, settings=settings):
            report.skipped_window += 1
            continue
        if event.cancelled:
            # Never written, never counted, and no warm-up generated for it —
            # but its warm-up is named here, because it was generated while the
            # game was on and has to come off with it.
            called_off.update((event.uid, warmup.uid_for(event.uid)))
            continue
        # Only the sync layer can see this: the adapter has no database. A feed
        # may well supply an address inline, so "has an address" proves nothing —
        # what matters is whether we know the place, which is what carries a
        # confirmed pin and survives the venue being spelled differently later.
        if _enrich_venue(conn, event):
            placed.add(event.uid)
        elif (not withheld.is_withheld(event.uid, off)
                and "location" not in overrides.get(event.uid, {})):
            unresolved_venues.add(event.venue.name or event.venue.raw)
        report.events_seen += 1
        if event.is_game:
            report.fixtures_seen += 1
        # Matching `routing.collection_for`'s precedence, staging included: a
        # source still being onboarded is already held somewhere, so counting
        # its events as "awaiting review" would report a hold that is not
        # happening and send somebody to a page with nothing on it.
        if (event.needs_enrichment and settings.enrichment_collection
                and not source.staging_collection):
            report.awaiting_review += 1
        events.append(event)

    if unresolved_venues:
        report.diagnostics["unresolved_venues"] = sorted(unresolved_venues)

    # After the loop above, deliberately. The warm-up copies its game's venue,
    # so it wants the enriched one; and it must not be counted again by any of
    # the tallies in there — `fixtures_seen` is about the feed's fixtures, and
    # `awaiting_review` is a number of *questions* a person has to answer, which
    # a held game and its held warm-up do not double.
    #
    # The overlay goes on first, so a warm-up is built from the *corrected*
    # game: moved with its start, and at its venue. Once, and only over the
    # corrected events — a second expansion over the feed's would give each
    # warm-up two versions of its content, and the receipt comparison below
    # would find them different on every poll for as long as the override
    # stood. The source events are kept beside them, because `event_content`
    # records what the source said and nothing else.
    sourced = {event.uid: event for event in events}
    events = [overlay.apply(event, overrides.get(event.uid)) for event in events]
    events = warmup.expand(events, minutes=activity.warmup_minutes)
    for event in events:
        # The arrival time is derived, like the warm-up it matches, so it is
        # recorded as derived from the start the calendar shows. The source
        # event would otherwise state an arrival for a kick-off nobody is
        # going to.
        original = sourced.get(event.uid)
        if original is not None and original is not event:
            original.arrive_at = event.arrive_at

    # --- other teams at the same meet (`correlate.py`) ---------------------
    # Stored and committed before any write or delete, for `approve_removals`'
    # reason: a partner reading it must see what the deletes below act on.
    mine: dict[str, dict] = {}
    if source.enabled and not source.staging_collection:
        for event in events:
            if event.needs_enrichment or withheld.is_withheld(event.uid, off):
                continue
            own = kids_for(event.uid)
            k = correlate.key(event, activity.sport,
                              collection_for(event, activity, own[0], settings))
            if k is None:
                continue
            named = event.venue is not None and event.uid in placed
            mine[event.uid] = {
                "uid": event.uid, "source_id": source.id, "key": k,
                "kids": ",".join(c.id for c in own),
                "kid_order": min(c.birth_order for c in own),
                "body": event.body, "url": event.url, "kit": event.kit,
                "arrive_at": event.arrive_at.isoformat() if event.arrive_at else None,
                "venue": event.venue.name if named else None,
                "field": event.venue.field if event.venue else None,
            }

    # --- diff --------------------------------------------------------------
    max_pct, max_count = _guard_thresholds(source, settings)
    # Same lower bound the incoming events were filtered by, so an event ageing
    # out of the window is not mistaken for one that was cancelled.
    known = repo.known_hashes(
        conn, source.id,
        since=(now - timedelta(days=settings.sync_window_back_days)).isoformat(),
    )
    delta = diff_poll(
        events, known, now=now, max_pct=max_pct, max_count=max_count,
        # A warm-up is derived from a game, not read from the feed, so it says
        # nothing about whether this fetch can be trusted. See `diff_poll`.
        counts_as_evidence=lambda uid: not warmup.is_synthetic(uid),
        called_off=called_off,
    )

    report.unchanged = len(delta.unchanged)

    before = repo.source_candidates(conn, source.id)
    # A held poll keeps what was stored: a suspect fetch must not reshape a group.
    candidates = before if delta.is_anomalous else sorted(mine.values(), key=lambda r: r["uid"])
    if not dry_run:
        repo.replace_candidates(conn, source.id, candidates)
        conn.commit()
    # A dry run has not stored its own rows, so they are laid over the others'.
    groups = repo.candidate_groups(conn, {r["key"] for r in candidates})
    merges: dict[str, correlate.Merged] = {}
    dropped: set[str] = set()
    for k in {r["key"] for r in candidates}:
        members = [r for r in groups.get(k, []) if r["source_id"] != source.id]
        members += [r for r in candidates if r["key"] == k]
        if not correlate.usable(members):
            continue
        lead = correlate.carrier(r["source_id"] for r in members)
        for r in members:
            if r["source_id"] != source.id:
                continue
            if lead != source.id:
                dropped.add(r["uid"])
            else:
                merges[r["uid"]] = merged = correlate.merge(members)
                merged_kids[r["uid"]] = sorted(
                    (repo.get_child(conn, i) for i in merged.kids),
                    key=lambda c: (c.birth_order, c.name))
    # Dropped like a withheld event, and never stored as one: only a person sets
    # `event_state.withheld`, and this copy returns the moment the group ends.
    off = off | dropped

    if not dry_run and delta.confirm(confirm_held):
        report.confirmed = len(delta.cancelled)
        # Recorded and committed before a single delete is sent. The Mac mirror
        # reads the calendar server and asks `GET /v1/placements` to account for
        # what is missing; an approval written after the deletes would leave a
        # window where the absence is unexplained and the mirror holds it —
        # asking a second person for a decision this one has just made.
        repo.approve_removals(conn, delta.cancelled)
        conn.commit()

    if delta.is_anomalous:
        report.status = "held"
        report.held = delta.anomaly
        report.held_kind = delta.anomaly_kind
        report.held_cancellations = list(delta.held_cancellations)
        if delta.held_cancellations:
            report.held_fingerprint = fingerprint(delta.held_cancellations)
        # An identity break withholds creations too, so there may be nothing
        # left to apply. A disappearance still has valid creates and updates:
        # the events that ARE present are real, only their absence is suspect.

    def _kept(events: list[Event]) -> list[Event]:
        """``events``, minus the ones somebody has taken off the calendar.

        A withheld event stays in the diff — it is still in the feed, and
        removing it there would make it look like a disappearance and count
        against the guard. It is dropped here instead, on the way to the write.
        """
        return [e for e in events if not withheld.is_withheld(e.uid, off)]

    if dry_run:
        report.created = len(_kept(delta.created))
        report.updated = len(_kept(delta.updated))
        report.cancelled = len(delta.cancelled) + len(delta.called_off)
        report.called_off = len(delta.called_off)
        report.withheld = len(delta.created) + len(delta.updated) \
            - report.created - report.updated
        return report

    if delta.is_anomalous:
        repo.record_poll_run(
            conn, source_id=source.id, status="held",
            detail=delta.anomaly, raw_sha256=result.raw_sha256,
        )

    # Committed before anything is written or deleted, for the same reader and
    # the same reason as a person's approval above.
    if delta.called_off:
        repo.note_cancelled_upstream(conn, delta.called_off)
        conn.commit()

    # --- write, then record ------------------------------------------------
    contents = repo.event_contents(conn, source.id)

    fresh = {e.uid for e in delta.created}
    pending = delta.created + delta.updated

    # Placement is checked independently of content. Staging a source, promoting
    # it, or changing collection_template all move an event without the feed
    # changing at all — and the diff only ever sees content, so those events come
    # back "unchanged" and would silently stay where they were. Re-writing them
    # is what makes promotion actually relocate anything.
    #
    # Stored content is checked the same way and for the same reason. The diff's
    # hash is taken over the *raw feed component*, before this module enriches a
    # venue from the venue table — so teaching an alias or confirming an address
    # changes what an event renders to while leaving its hash untouched, and the
    # event comes back "unchanged" with the correction never reaching anyone's
    # phone. Comparing what we stored against what the feed now derives to is
    # what closes that gap, and it is also what backfills rows written before
    # `event_content` existed: no content stored is a difference like any other.
    #
    # Overrides are the third member of the family, and checked the same way
    # for the same reason: a correction changes what an event renders to while
    # its hash, which is the feed's, stays exactly where it was. What is
    # compared is the *sum* of the layers (`overlay.project`) — what was last
    # written against what would be written now — so lifting an override that
    # says what the feed now says costs no rewrite of anybody's calendar.
    moved_uids: set[str] = set()
    refreshed_uids: set[str] = set()
    amended_uids: set[str] = set()
    primary_child = min(children, key=lambda c: (c.birth_order, c.name))
    for event in delta.unchanged:
        state = states.get(event.uid)
        if state is None or state.cancelled:
            continue
        belongs = collection_for(
            event, activity, primary_child, settings,
            override=source.staging_collection,
        )
        original = sourced.get(event.uid, event)
        current = overrides.get(event.uid)
        stored = contents.get(event.uid)
        now_content = repo.content_of(original)
        if belongs != state.collection:
            pending.append(event)
            moved_uids.add(event.uid)
        elif (state.kids or (activity.child_id,)) != tuple(c.id for c in kids_for(event.uid)):
            # Who is going changed — an absence, or a kid added to the team —
            # and the title says who.
            pending.append(event)
            refreshed_uids.add(event.uid)
        elif state.merged != (merges[event.uid].fingerprint if event.uid in merges else None):
            # Another team's copy of this meet changed what is merged into it.
            pending.append(event)
            refreshed_uids.add(event.uid)
        elif stored is None:
            pending.append(event)
            refreshed_uids.add(event.uid)
        elif (overlay.project(stored, state.starts_at, state.applied_overlay)
              != overlay.project(now_content, original.starts_at.isoformat(), current)):
            pending.append(event)
            if overlay.values(state.applied_overlay) != overlay.values(current):
                amended_uids.add(event.uid)
            else:
                refreshed_uids.add(event.uid)
        elif (stored != now_content
              or overlay.values(state.applied_overlay) != overlay.values(current)):
            # The layers moved and their sum did not. Nothing to send; the
            # receipts are brought up to date so they say who owns the value.
            repo.record_receipt_only(
                conn, uid=event.uid, content=now_content,
                applied_overlay=current, observed_at=now.isoformat(),
            )

    # Last, after every reason to write an event has been collected and before
    # any of them is acted on. A withheld event is skipped whether it is new,
    # changed, moved or merely re-rendered: the decision is about the event, not
    # about what the feed did to it this morning.
    report.withheld = len(pending) - len(_kept(pending))
    pending = _kept(pending)

    for event in pending:
        previous_state = states.get(event.uid)
        previous_ref = None
        if previous_state is not None and not previous_state.cancelled:
            previous_ref = TargetRef(
                collection=previous_state.collection,
                remote_id=previous_state.remote_id or event.uid,
                etag=previous_state.remote_etag,
            )
        merge = merges.get(event.uid)
        shown = event if merge is None else replace(
            event, body=merge.body, kit=merge.kit, arrive_at=merge.arrive_at)
        try:
            rendered = render(
                shown, activity, kids_for(event.uid), settings,
                alarm_minutes=activity.alarm_minutes(is_game=event.alarms_as_game),

                collection_override=source.staging_collection,
            )
            target.ensure_collection(rendered.collection)
            ref = target.upsert(rendered, previous_ref)
        except TargetError as exc:
            report.status = "error"
            report.errors.append(f"{event.uid}: {exc}")
            continue

        # The source's start and content, the overrides laid over them, and
        # the start the calendar now shows — `event` is the corrected copy and
        # `original` what the feed said. A warm-up has no source of its own:
        # it is both.
        original = sourced.get(event.uid, event)
        repo.record_event_state(
            conn,
            uid=event.uid,
            source_id=source.id,
            collection=ref.collection,
            remote_id=ref.remote_id,
            content_hash=event.content_hash or "",
            remote_etag=ref.etag,
            starts_at=original.starts_at.isoformat(),
            shown_starts_at=event.starts_at.isoformat(),
            applied_overlay=overrides.get(event.uid),
            kids=tuple(c.id for c in kids_for(event.uid)),
            merged=merge.fingerprint if merge else None,
        )
        # Placement and content are recorded together, behind the same barrier:
        # both describe a write the target has just accepted, and a copy of the
        # event that could outrun the calendar would be the one new failure mode
        # storing content introduces (docs/API.md, "Before any of this can be
        # built"). What is stored is what the *source* said — never what was
        # rendered — so that a later amendment layered on top survives the next
        # poll rather than being reverted by it (docs/MATRIX.md §4).
        repo.record_event_content(
            conn,
            uid=event.uid,
            content=repo.content_of(original),
            observed_at=now.isoformat(),
        )
        # Counted by how the diff classified it, not by whether a row existed:
        # a resurrected event has a (cancelled) row but is genuinely new again.
        if event.uid in fresh:
            report.created += 1
        elif event.uid in refreshed_uids:
            report.refreshed += 1
            report.unchanged -= 1
        elif event.uid in amended_uids:
            report.amended += 1
            report.unchanged -= 1
        else:
            report.updated += 1
            if moved_uids and event.uid in moved_uids:
                report.moved += 1
                report.unchanged -= 1

    feed_said = set(delta.called_off)
    for uid in delta.cancelled + delta.called_off:
        state = states.get(uid)
        if state is None:
            continue
        try:
            target.cancel(
                TargetRef(
                    collection=state.collection,
                    remote_id=state.remote_id or uid,
                    etag=state.remote_etag,
                )
            )
        except TargetError as exc:
            report.status = "error"
            report.errors.append(f"{uid}: {exc}")
            continue
        repo.mark_event_cancelled(conn, uid)
        report.cancelled += 1
        if uid in feed_said:
            report.called_off += 1
        else:
            # Gone, rather than called off: a person's fix to it has nowhere
            # to go until somebody says where — or the same event comes back.
            # Only once the target has accepted the delete, and so never for a
            # held poll, whose missing events are not known to be gone at all.
            report.fixes_orphaned += repo.mark_orphaned(conn, uid)

    _judge_overrides(conn, source.id, sourced, now=now, report=report)

    # Whatever is withheld and still on the calendar comes off now — the same
    # call the console makes the moment somebody presses the button, repeated
    # here because the calendar server may have been unreachable at that moment.
    # After the target has accepted the removal, `known_hashes` stops seeing the
    # row at all, so later polls take the cheap path above and never reach this.
    # Its own read of `event_state`, not the `states` above: the loop just
    # above marked rows cancelled, and a stale copy would ask the target to
    # delete an event it has already deleted.
    removal = withheld.enforce(conn, source.id, target, also=dropped)
    report.cancelled += removal.removed
    if not removal.ok:
        report.status = "error"
        report.errors.extend(removal.errors)

    # Stored content is kept only for as long as the calendar keeps the event.
    # Same bound the diff already compares across, which is what stops a prune
    # from ever looking like a change: an event below it is filtered out of both
    # sides before it reaches this loop.
    horizon = (now - timedelta(days=settings.sync_window_back_days)).isoformat()
    repo.prune_event_content(conn, before=horizon)
    repo.prune_overrides(conn, before=horizon)

    # --- record the run ----------------------------------------------------
    if report.status == "error":
        detail = "; ".join(report.errors)[:2000]
        repo.record_poll_run(
            conn, source_id=source.id, status="error",
            detail=detail, raw_sha256=result.raw_sha256,
        )
        repo.record_source_error(conn, source.id, detail)
    elif not delta.is_anomalous:
        # What was done, not what the diff proposed: the diff counts a withheld
        # event the feed still publishes as new on every poll, and the log said
        # "3 new" for weeks while nothing was written.
        detail = report.tally()
        if report.confirmed:
            detail += f" ({report.confirmed} held, confirmed by a person)"
        repo.record_poll_run(
            conn, source_id=source.id, status="ok",
            detail=detail, raw_sha256=result.raw_sha256,
        )
        repo.record_source_success(conn, source.id)

    conn.commit()

    # Whoever shares a meet with this source, old key or new, now sees a
    # different group; one run each is enough for them to converge.
    if follow and not dry_run and before != candidates:
        synced, errors = sync_partners(
            conn, source.id, {r["key"] for r in before} | {r["key"] for r in candidates},
            target, now=now, secrets=secrets, fetcher=fetcher)
        report.partners_synced.extend(synced)
        report.errors.extend(errors)
    return report


def _judge_overrides(conn, source_id: str, sourced: dict[str, Event], *,
                     now: datetime, report: SyncReport) -> None:
    """Hold each of a person's fixes up against what the feed says now.

    The only writes a poll makes to the overlay, and none of them changes what
    a fix says (`overlay.py`):

    - **The feed has caught up** — it now says what the fix says. The fix is
      lifted. Safe to do without anybody: an exact match changes nothing on
      the calendar, and one through a venue alias names the same place, which
      the next poll then writes in the feed's spelling.
    - **The feed has moved somewhere else** — neither its old value nor the
      fix. The fix stays on the calendar and is marked for a person to decide;
      only they can tell which of the two is true.
    - **The feed has gone back** to what it said when the fix was set. The
      question answers itself, and the mark comes off quietly.

    Judged after the writes, against events actually in this poll. An event
    missing from it — out of the window, called off, withheld by a guard —
    says nothing about whether its fix is still needed, and is left alone.
    """
    place = lambda name: repo.resolve_venue_alias(conn, name)  # noqa: E731
    states = repo.event_states(conn, source_id)
    for item in repo.override_rows(conn, source_id):
        event = sourced.get(item.uid)
        if event is None:
            continue
        if item.status == "orphaned":
            # The same event came back under the same id: exact identity, so
            # nothing to ask. Judged below like any other from here.
            repo.mark_settled(conn, item, action="returned")
            item = replace(item, status="active")
        upstream = repo.unit_value(event, item.unit)
        verdict = overlay.judge(item.unit, value=item.value, baseline=item.baseline,
                                upstream=upstream, place=place)
        if verdict == overlay.CONVERGED:
            raw = event.venue.raw if item.unit == "location" and event.venue else None
            exact = overlay.same(item.unit, upstream, item.value)
            if repo.clear_converged(conn, item, upstream=upstream, raw=raw, exact=exact):
                report.fixes_cleared += 1
        elif verdict == overlay.UNCHANGED:
            if item.status == "changed":
                repo.mark_settled(conn, item, action="settled")
        elif not retire.upcoming(states[item.uid].shown_starts_at, now):
            # A game already played: nobody can act on the disagreement, and
            # asking would be a question with no useful answer. By the start
            # the calendar shows — a game the feed has as yesterday and a
            # person moved to next week has not been played.
            continue
        elif not (item.status == "changed"
                  and overlay.matches(item.unit, upstream, item.upstream_seen, place=place)):
            # Once per new value. The same disagreement on every poll is one
            # question, not one every twenty minutes.
            if repo.mark_changed(conn, item, upstream=upstream, at=now.isoformat()):
                report.fixes_changed += 1


def sync_all(
    conn, target, *, now: datetime, secrets: SecretStore | None = None,
    fetcher=http_fetch, dry_run: bool = False, only: str | None = None,
) -> list[SyncReport]:
    reports = []
    for source in repo.list_sources(conn, enabled_only=True):
        if only and source.id != only:
            continue
        reports.append(
            sync_source(
                conn, source, target, now=now, secrets=secrets,
                fetcher=fetcher, dry_run=dry_run,
            )
        )
    return reports


class NoTarget:
    """Stands in for a calendar during a preview.

    ``sync_source(dry_run=True)`` returns before it touches a target. Passing
    something that raises rather than something that writes means that if the
    ordering in the sync loop ever changes, a preview fails loudly instead of
    quietly writing to the family's real calendar.
    """

    def __getattr__(self, name):
        def refuse(*_args, **_kwargs):
            raise RuntimeError(
                f"a dry run reached target.{name}(); previews must never write"
            )

        return refuse


def sync_partners(conn, source_id: str, keys, target, *, now: datetime,
                  secrets: SecretStore | None = None, fetcher=http_fetch
                  ) -> tuple[list[str], list[str]]:
    """Sync every other source in the groups under ``keys``, once each.

    Returns the ids synced and their errors. They run with ``follow=False``, so
    it goes one hop and stops.
    """
    # A paused partner keeps its candidates but is never synced: that would
    # put a stopped team back on the calendar.
    partners = sorted({r["source_id"] for rows in repo.candidate_groups(conn, keys).values()
                       for r in rows if repo.get_source(conn, r["source_id"]).enabled}
                      - {source_id})
    errors: list[str] = []
    for partner_id in partners:
        sub = sync_source(conn, repo.get_source(conn, partner_id), target, now=now,
                          secrets=secrets, fetcher=fetcher, follow=False)
        errors.extend(f"{partner_id}: {e}" for e in sub.errors)
    return partners, errors
