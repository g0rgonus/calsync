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
    held: str | None = None
    held_kind: str | None = None
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = [
            f"{self.source_id}: {self.status}",
            f"{self.created} new",
            f"{self.updated} changed",
            f"{self.unchanged} unchanged",
            f"{self.cancelled} cancelled",
        ]
        if self.skipped_window:
            parts.append(f"{self.skipped_window} outside window")
        if self.held:
            parts.append(f"HELD ({self.held_kind}): {self.held}")
        parts.extend(f"ERROR: {e}" for e in self.errors)
        return ", ".join(parts)


def _in_window(event: Event, *, now: datetime, settings: Settings) -> bool:
    """Keep the calendar to a useful span.

    Past events age out of feeds normally; backfilling years of them into a
    calendar the family actually reads is noise, not history.
    """
    earliest = now - timedelta(days=settings.sync_window_back_days)
    latest = now + timedelta(days=settings.sync_window_forward_days)
    return earliest <= event.starts_at <= latest


def _enrich_venue(conn, event: Event) -> None:
    """Upgrade a parsed venue to a known one, in place.

    The adapter can only split a free-text string; coordinates come from the
    venue tables. Checked by raw string first, then by parsed name, because the
    alias table records exactly the strings seen in the wild.
    """
    if event.venue is None:
        return
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
            return


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
    report = SyncReport(source_id=source.id)
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
        result = sources.parse(source.kind, raw, activity, source_id=source.id)
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

    events = []
    for event in result.events:
        if not _in_window(event, now=now, settings=settings):
            report.skipped_window += 1
            continue
        _enrich_venue(conn, event)
        events.append(event)

    # --- diff --------------------------------------------------------------
    max_pct, max_count = _guard_thresholds(source, settings)
    known = repo.known_hashes(conn, source.id)
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
    for event in delta.created + delta.updated:
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
