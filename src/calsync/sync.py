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

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import repo, sources
from .diff import diff_poll
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
    #: Events relocated because their collection changed, not their content —
    #: promotion off the onboarding calendar, or a routing template change.
    moved: int = 0
    held: str | None = None
    held_kind: str | None = None
    errors: list[str] = field(default_factory=list)

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
            and self.fixtures_seen > 0
        )

    def line(self) -> str:
        parts = [
            f"{self.source_id}: {self.status}",
            f"{self.created} new",
            f"{self.updated} changed",
            f"{self.unchanged} unchanged",
            f"{self.cancelled} cancelled",
        ]
        if self.moved:
            parts.append(f"{self.moved} moved")
        if self.skipped_window:
            parts.append(f"{self.skipped_window} outside window")
        if self.staged_to:
            parts.append(f"staged to {self.staged_to!r}")
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
) -> SyncReport:
    """Poll one source and reconcile it into ``target``.

    ``raw`` bypasses the network with a payload already in hand — how the golden
    tests run, and how a saved feed can be replayed without a credential.
    """
    report = SyncReport(source_id=source.id, staged_to=source.staging_collection)
    settings = Settings.load(conn)
    activity: Activity = repo.get_activity(conn, source.activity_id)
    # One child per activity: the schema binds them 1:1. Shared events (two kids
    # on one team) need a join table that does not exist yet — until it does,
    # multi-kid titles are only reachable through the amendment path.
    children = [repo.get_child(conn, activity.child_id)]

    # --- fetch + parse: any failure aborts before the diff ------------------
    try:
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

    events = []
    unresolved_venues: set[str] = set()
    for event in result.events:
        if not _in_window(event, now=now, settings=settings):
            report.skipped_window += 1
            continue
        # Only the sync layer can see this: the adapter has no database. A feed
        # may well supply an address inline, so "has an address" proves nothing —
        # what matters is whether we know the place, which is what carries a
        # confirmed pin and survives the venue being spelled differently later.
        if not _enrich_venue(conn, event):
            unresolved_venues.add(event.venue.name or event.venue.raw)
        if event.is_game:
            report.fixtures_seen += 1
        events.append(event)

    if unresolved_venues:
        report.diagnostics["unresolved_venues"] = sorted(unresolved_venues)

    # --- diff --------------------------------------------------------------
    max_pct, max_count = _guard_thresholds(source, settings)
    # Same lower bound the incoming events were filtered by, so an event ageing
    # out of the window is not mistaken for one that was cancelled.
    known = repo.known_hashes(
        conn, source.id,
        since=(now - timedelta(days=settings.sync_window_back_days)).isoformat(),
    )
    delta = diff_poll(events, known, now=now, max_pct=max_pct, max_count=max_count)

    report.unchanged = len(delta.unchanged)

    if delta.is_anomalous:
        report.status = "held"
        report.held = delta.anomaly
        report.held_kind = delta.anomaly_kind
        # An identity break withholds creations too, so there may be nothing
        # left to apply. A disappearance still has valid creates and updates:
        # the events that ARE present are real, only their absence is suspect.

    if dry_run:
        report.created = len(delta.created)
        report.updated = len(delta.updated)
        report.cancelled = len(delta.cancelled)
        return report

    if delta.is_anomalous:
        repo.record_poll_run(
            conn, source_id=source.id, status="held",
            detail=delta.anomaly, raw_sha256=result.raw_sha256,
        )

    # --- write, then record ------------------------------------------------
    states = repo.event_states(conn, source.id)

    fresh = {e.uid for e in delta.created}
    pending = delta.created + delta.updated

    # Placement is checked independently of content. Staging a source, promoting
    # it, or changing collection_template all move an event without the feed
    # changing at all — and the diff only ever sees content, so those events come
    # back "unchanged" and would silently stay where they were. Re-writing them
    # is what makes promotion actually relocate anything.
    moved_uids: set[str] = set()
    primary_child = min(children, key=lambda c: (c.birth_order, c.name))
    for event in delta.unchanged:
        state = states.get(event.uid)
        if state is None or state.cancelled:
            continue
        belongs = collection_for(
            event, activity, primary_child, settings,
            override=source.staging_collection,
        )
        if belongs != state.collection:
            pending.append(event)
            moved_uids.add(event.uid)

    for event in pending:
        previous_state = states.get(event.uid)
        previous_ref = None
        if previous_state is not None and not previous_state.cancelled:
            previous_ref = TargetRef(
                collection=previous_state.collection,
                remote_id=previous_state.remote_id or event.uid,
                etag=previous_state.remote_etag,
            )
        try:
            rendered = render(
                event, activity, children, settings,
                alarm_minutes=activity.alarm_minutes(is_game=event.is_game),
                collection_override=source.staging_collection,
            )
            target.ensure_collection(rendered.collection)
            ref = target.upsert(rendered, previous_ref)
        except TargetError as exc:
            report.status = "error"
            report.errors.append(f"{event.uid}: {exc}")
            continue

        repo.record_event_state(
            conn,
            uid=event.uid,
            source_id=source.id,
            collection=ref.collection,
            remote_id=ref.remote_id,
            content_hash=event.content_hash or "",
            remote_etag=ref.etag,
            starts_at=event.starts_at.isoformat(),
        )
        # Counted by how the diff classified it, not by whether a row existed:
        # a resurrected event has a (cancelled) row but is genuinely new again.
        if event.uid in fresh:
            report.created += 1
        else:
            report.updated += 1
            if moved_uids and event.uid in moved_uids:
                report.moved += 1
                report.unchanged -= 1

    for uid in delta.cancelled:
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

    # --- record the run ----------------------------------------------------
    if report.status == "error":
        detail = "; ".join(report.errors)[:2000]
        repo.record_poll_run(
            conn, source_id=source.id, status="error",
            detail=detail, raw_sha256=result.raw_sha256,
        )
        repo.record_source_error(conn, source.id, detail)
    elif not delta.is_anomalous:
        repo.record_poll_run(
            conn, source_id=source.id, status="ok",
            detail=delta.summary(), raw_sha256=result.raw_sha256,
        )
        repo.record_source_success(conn, source.id)

    conn.commit()
    return report


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
