"""Teams at one meet or practice write it once, as a step after every sync.

`publish` reads only the database: each team's sync stored its candidates
(`shared_candidates`), and the merged events live under a hidden companion
source of the carrier team (`repo.ensure_companion`). It fetches nothing, except
when a group ends and a member's own copy has to be restored.

The order is the safety, and every step below leaves a kid's event on the
calendar if the next one fails:

- a member copy comes off only after its merged replacement was accepted by the
  target and recorded (`merged_members` is written after that, `enforce` after it);
- a merged event is cancelled only after the members' own copies are back.

Nothing here sets `event_state.withheld`: only a person does. A claimed copy
is dropped by being in `merged_members` while its merged event is live.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from . import correlate, repo, sources, warmup, withheld
from .diff import diff_poll, fingerprint
from .fetch import http_fetch
from .render import render
from .secrets import SecretStore
from .settings import Settings
from .targets import TargetError, TargetRef


LEASE = timedelta(minutes=5)   # longer than any run; a crashed one frees it


@dataclass
class MergeReport:
    written: int = 0
    ended: int = 0
    errors: list[str] = field(default_factory=list)
    #: Another merge step held the lease, so this one did nothing.
    skipped: bool = False
    #: Not errors: a state that settles on its own, worth saying where a person looks.
    notes: list[str] = field(default_factory=list)

    def said(self) -> list[str]:
        return self.errors + self.notes


@dataclass
class _Group:
    uid: str                # the merged event's uid
    lead: dict              # the carrier's candidate row
    rows: list[dict]        # every member's candidate row, kids after absences
    merged: correlate.Merged
    kids: tuple[str, ...]   # in birth order
    game: object = None
    warmup: object = None
    content: dict = None


def _members(conn) -> list[dict]:
    """Candidates that could be in a group: not withheld, with a kid still going."""
    rows = repo.all_candidates(conn)
    off = {r["uid"] for r in conn.execute("SELECT uid FROM event_state WHERE withheld IS NOT NULL")}
    away = repo.absences_of(conn, [r["uid"] for r in rows])
    out = []
    for r in rows:
        if r["uid"] in off:
            continue
        kids = [k for k in r["kids"].split(",") if k and k not in away.get(r["uid"], ())]
        if kids:
            out.append({**r, "kids": ",".join(kids)})
    return out


def _desired(conn, rows: list[dict]) -> list[_Group]:
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)
    groups = []
    for members in sorted(by_key.values(), key=lambda ms: min(r["uid"] for r in ms)):
        if not correlate.usable(members):
            continue
        lead = min(members, key=lambda r: r["source_id"])
        merged = correlate.merge(members)
        kids = tuple(c.id for c in sorted(
            (repo.get_child(conn, i) for i in merged.kids),
            key=lambda c: (c.birth_order, c.name)))
        groups.append(_Group("", lead, members, merged, kids))
    _assign_uids(groups, repo.all_members(conn))
    return sorted(groups, key=lambda g: g.uid)


def _assign_uids(groups: list[_Group], members: dict) -> None:
    """Give each group a merged uid no other group has.

    A group keeps a uid its members already hold, so a team joining or leaving
    never re-creates the event on phones or undoes a person's withhold. When a
    group splits, a held uid goes first to the group with the member it was made
    from; the rest take another held uid or a fresh one, never one already given.
    """
    current = {m: uid for uid, ms in members.items() for m, _ in ms}
    held = [sorted({current[r["uid"]] for r in g.rows if r["uid"] in current})
            for g in groups]
    taken: set[str] = set()
    for g, uids in zip(groups, held):
        own = [u for u in uids if u in {f"merged.{r['uid']}" for r in g.rows}]
        if own and own[0] not in taken:
            g.uid = own[0]
            taken.add(g.uid)
    for g, uids in zip(groups, held):
        if not g.uid:
            spare = [u for u in uids if u not in taken]
            if spare:
                g.uid = spare[0]
                taken.add(g.uid)
    claimed = set(current.values())
    for g in groups:
        if g.uid:
            continue
        fresh = [f"merged.{r['uid']}" for r in sorted(g.rows, key=lambda r: (r["source_id"], r["uid"]))]
        # Not one another group's members still hold: that one is ending or moving.
        free = [u for u in fresh if u not in taken and u not in claimed]
        if free:
            g.uid = free[0]
            taken.add(g.uid)


def _build(conn, group: _Group, companion: repo.Source) -> list:
    """The merged event and its warm-up, as they should be on the calendar."""
    activity = repo.get_activity(conn, companion.activity_id)
    seed = hashlib.sha256(group.lead["content"].encode()).hexdigest()[:32]
    event = repo.event_from_candidate(
        group.lead["content"], uid=group.uid, source_id=group.lead["source_id"],
        content_hash=seed)
    group.content = repo.content_of(event)
    out = warmup.expand([event], minutes=activity.warmup_minutes)
    if len(out) > 1 and group.merged.arrive_at:
        # The earliest team's arrival, so the warm-up starts where the body says.
        out[1].starts_at = group.merged.arrive_at
    if len(out) > 1:
        out[1].content_hash = hashlib.sha256(
            f"{seed}\x1e{out[1].starts_at.isoformat()}".encode()).hexdigest()[:32]
    group.game, group.warmup = out[0], (out[1] if len(out) > 1 else None)
    return out


def _live(state) -> bool:
    return state is not None and not state.cancelled


def _cancel(conn, target, uid: str, report: MergeReport) -> bool:
    state = repo.event_state(conn, uid)
    if state is None or state.cancelled:
        return True
    try:
        target.cancel(TargetRef(collection=state.collection,
                                remote_id=state.remote_id or uid, etag=state.remote_etag))
    except TargetError as exc:
        report.errors.append(f"{uid}: {exc}")
        return False
    repo.mark_event_cancelled(conn, uid)
    return True


def publish(conn, target, *, now: datetime, secrets: SecretStore | None = None,
            fetcher=http_fetch) -> MergeReport:
    """Bring the merged events in line with the stored candidates."""
    return _leased(conn, lambda keep: _publish(conn, target, now=now, secrets=secrets,
                                               fetcher=fetcher, keep=keep), rerun=True)


class _LeaseLost(Exception):
    """Another run took the lease over; this one stops between groups."""


def _leased(conn, run, *, rerun: bool) -> MergeReport:
    """Run while holding the lease; a run that finds it held asks the holder to go again.

    Asking, then trying once more, leaves no gap: either the holder sees the request
    after releasing, or it has already released and the second try succeeds. Taking
    the lease clears the request in the same statement, so no run can clear a
    request it then fails to act on.
    """
    token = uuid.uuid4().hex

    def take(rerun_only: bool = False) -> bool:
        # Wall time, not the sync's clock: the lease is about processes, not the schedule.
        wall = datetime.now(timezone.utc)
        return repo.take_merge_lease(conn, token=token, now=wall.isoformat(),
                                     until=(wall + LEASE).isoformat(), rerun_only=rerun_only)

    def keep() -> None:
        until = (datetime.now(timezone.utc) + LEASE).isoformat()
        if not repo.renew_merge_lease(conn, token=token, until=until):
            raise _LeaseLost()

    if not take():
        if not rerun:
            return MergeReport(skipped=True)
        repo.request_merge_rerun(conn)
        if not take():
            return MergeReport(skipped=True)
    report = MergeReport()
    passes = 3   # bounded: a steady stream of requests cannot hold a run forever
    for n in range(passes):
        try:
            part = run(keep)
        except _LeaseLost:
            part = MergeReport(errors=["merge step stopped: another run took over"])
        finally:
            repo.release_merge_lease(conn, token=token)
        report.written += part.written
        report.ended += part.ended
        report.errors.extend(part.errors)
        report.notes.extend(part.notes)
        # The last pass never takes the lease again: a request still pending stays
        # set for the next merge step instead of holding the lease to its expiry.
        if rerun and n == passes - 1 and repo.merge_rerun_pending(conn):
            report.notes.append("a change is queued for the next merge step")
        if not rerun or n == passes - 1 or not take(rerun_only=True):
            break
    return report


def _publish(conn, target, *, now: datetime, secrets: SecretStore | None = None,
             fetcher=http_fetch, keep=lambda: None) -> MergeReport:
    from . import sync   # imports this module for its own run

    report = MergeReport()
    settings = Settings.load(conn)
    groups = _desired(conn, _members(conn))
    waiting = [g for g in groups if not g.uid]
    if waiting:
        # Its members keep their own copies until the uid it needs is free.
        report.notes.append(f"{len(waiting)} group(s) wait for a free merged uid until "
                            "the next merge step")
    groups = [g for g in groups if g.uid]
    # Created on demand, committed before anything is written under it.
    # An existing merged event stays with the companion that holds it.
    needed = {}
    for g in groups:
        state = repo.event_state(conn, g.uid)
        needed[g.uid] = (repo.get_source(conn, state.source_id) if state is not None
                         else repo.ensure_companion(conn, g.lead["source_id"]))
    conn.commit()
    floor = (now - timedelta(days=settings.sync_window_back_days)).isoformat()

    plans = []   # (companion, desired events, diff, states, contents)
    for companion in repo.companions(conn):
        mine, events = [], []
        for g in (g for g in groups if needed[g.uid].id == companion.id):
            built = _build(conn, g, companion)
            # Below the window the diff does not look either, same as a sync.
            if built[0].starts_at.isoformat() >= floor:
                mine.append(g)
                events.extend(built)
        max_pct, max_count = sync._guard_thresholds(companion, settings)
        delta = diff_poll(
            events, repo.known_hashes(conn, companion.id, since=floor), now=now,
            max_pct=max_pct, max_count=max_count,
            counts_as_evidence=lambda uid: not warmup.is_synthetic(uid))
        if delta.anomaly_kind == "disappearance":
            # A merged event is cancelled only once its members' own copies are back
            # (`_end`), so an end replaces rather than removes; a hold would only strand it.
            delta.confirm(fingerprint(delta.held_cancellations))
        if delta.is_anomalous:
            report.errors.append(f"{companion.id}: held, not cancelling: {delta.anomaly}")
        plans.append((companion, mine, delta, repo.event_states(conn, companion.id),
                      repo.event_contents(conn, companion.id)))

    # --- groups that form or change: merged event first, members after ----------
    for companion, mine, delta, states, contents in plans:
        activity = repo.get_activity(conn, companion.activity_id)
        changed = {e.uid for e in delta.created + delta.updated}
        for g in mine:
            keep()
            state = states.get(g.uid)
            ok = True
            if not (state is not None and state.withheld):
                for event in (g.game, g.warmup):
                    if event is None:
                        continue
                    ok &= _write(conn, target, report, companion, activity, settings, g,
                                 event, states.get(event.uid), contents.get(event.uid),
                                 force=event.uid in changed, now=now)
            fresh = repo.event_state(conn, g.uid)
            if not ok or fresh is None or not (_live(fresh) or fresh.withheld):
                continue   # nothing accepted, so no member copy may come off
            before = repo.members_of(conn, g.uid)
            repo.set_members(conn, g.uid, [(r["uid"], r["source_id"]) for r in g.rows])
            conn.commit()
            for sid in sorted({r["source_id"] for r in g.rows}):
                removal = withheld.enforce(conn, sid, target, also=repo.claimed_uids(conn, sid))
                conn.commit()
                report.errors.extend(removal.errors)
            # A member that left a group that lives on (its copy changed) is no longer
            # claimed, and nothing else would put its own copy back.
            claimed = {m for ms in repo.all_members(conn).values() for m, _ in ms}
            left = [m for m in before if m["member_uid"] not in claimed]
            if left and not _restore(conn, target, report, left, now=now, secrets=secrets,
                                     fetcher=fetcher):
                report.errors.append(f"{g.uid}: a team that left could not be restored")

    # --- groups that end: members back first, merged event after ---------------
    for companion, mine, delta, states, contents in plans:
        games = [u for u in delta.cancelled if not warmup.is_synthetic(u)]
        for uid in delta.cancelled:
            if warmup.is_synthetic(uid) and warmup.parent_of(uid) not in games:
                _cancel(conn, target, uid, report)
        for uid in games:
            keep()
            if _end(conn, target, report, uid, now=now, secrets=secrets, fetcher=fetcher):
                report.ended += 1

    # Claims left by a merged event that is off, not withheld and no longer wanted
    # (its group ended while it was withheld): give the members their copies back.
    desired = {g.uid for g in groups}
    for uid in sorted(repo.all_members(conn)):
        state = repo.event_state(conn, uid)
        if uid not in desired and not _live(state) and not (state and state.withheld):
            keep()
            _end(conn, target, report, uid, now=now, secrets=secrets, fetcher=fetcher)

    # --- a companion with no row at all and nothing to hold can go -------------
    wanted = {c.id for c in needed.values()}
    for companion in repo.companions(conn):
        if companion.id not in wanted and not conn.execute(
                "SELECT 1 FROM event_state WHERE source_id = ?", (companion.id,)).fetchone():
            repo.delete_companion(conn, companion.id)
    conn.commit()
    return report


def _write(conn, target, report, companion, activity, settings, g, event, state, stored,
           *, force: bool, now: datetime) -> bool:
    """Upsert one desired event if it differs from what is on the calendar."""
    is_game = event is g.game
    shown = event
    if is_game:
        shown = replace(event, body=g.merged.body, kit=g.merged.kit,
                        arrive_at=g.merged.arrive_at)
    layer = g.merged.layer if is_game else None
    content = g.content if is_game else repo.content_of(event)
    kids = [repo.get_child(conn, i) for i in g.kids]
    rendered = render(shown, activity, kids, settings,
                      alarm_minutes=activity.alarm_minutes(is_game=event.alarms_as_game))
    live = _live(state)
    if live and not force and (state.collection == rendered.collection
                               and state.merged == layer and state.kids == g.kids
                               and stored == content):
        return True
    previous = TargetRef(collection=state.collection, remote_id=state.remote_id or event.uid,
                         etag=state.remote_etag) if live else None
    try:
        target.ensure_collection(rendered.collection)
        ref = target.upsert(rendered, previous)
    except TargetError as exc:
        report.errors.append(f"{event.uid}: {exc}")
        return False
    repo.record_event_state(
        conn, uid=event.uid, source_id=companion.id, collection=ref.collection,
        remote_id=ref.remote_id, content_hash=event.content_hash or "",
        remote_etag=ref.etag, starts_at=event.starts_at.isoformat(),
        shown_starts_at=event.starts_at.isoformat(), kids=g.kids, merged=layer)
    repo.record_event_content(conn, uid=event.uid, content=content, observed_at=now.isoformat())
    conn.commit()
    report.written += 1
    return True


def _restore(conn, target, report, members, *, now, secrets, fetcher) -> bool:
    """Sync the teams of released members still going; True if every copy is back."""
    from . import sync

    candidates = {r["uid"] for r in repo.all_candidates(conn)}
    back = []
    for m in members:
        off = withheld.off_uids(conn, m["source_id"], repo.event_states(conn, m["source_id"]))
        if m["member_uid"] in candidates and m["member_uid"] not in off:
            back.append(m)
    for sid in sorted({m["source_id"] for m in back}):
        source = repo.get_source(conn, sid)
        if not source.enabled and source.kind not in sources.PUSHED:
            # A paused feed is not fetched for this; its copy waits, and the
            # merged event with it, until the team resumes.
            continue
        sub = sync.sync_source(conn, source, target, now=now, secrets=secrets,
                               fetcher=fetcher, publish=False)
        report.errors.extend(f"{sid}: {e}" for e in sub.errors)
    return all(_live(repo.event_state(conn, m["member_uid"])) for m in back)


def _end(conn, target, report, uid: str, *, now, secrets, fetcher) -> bool:
    """Retire a merged event whose group is gone, once its members are back."""
    members = repo.members_of(conn, uid)
    repo.drop_members(conn, uid)
    conn.commit()
    if not _restore(conn, target, report, members, now=now, secrets=secrets, fetcher=fetcher):
        # A member's own copy is not on the calendar, so the merged one stays.
        repo.set_members(conn, uid, [(m["member_uid"], m["source_id"]) for m in members])
        conn.commit()
        report.errors.append(f"{uid}: kept, a member's own copy could not be restored")
        return False
    ok = _cancel(conn, target, warmup.uid_for(uid), report)
    ok &= _cancel(conn, target, uid, report)
    conn.commit()
    return ok


def unmerge(conn, target, *, now: datetime, secrets: SecretStore | None = None,
            fetcher=http_fetch) -> MergeReport:
    """End every merged event and disable the companions, before a downgrade.

    A version without this module polls a companion as a team it cannot parse and
    never cancels its events. A merged event a person withheld passes that
    decision to the members' copies, so they stay off once they are back.
    """
    return _leased(conn, lambda keep: _unmerge(conn, target, now=now, secrets=secrets,
                                               fetcher=fetcher, keep=keep), rerun=False)


def _unmerge(conn, target, *, now: datetime, secrets: SecretStore | None = None,
             fetcher=http_fetch, keep=lambda: None) -> MergeReport:
    report = MergeReport()
    for companion in repo.companions(conn):
        for uid, state in sorted(repo.event_states(conn, companion.id).items()):
            if warmup.is_synthetic(uid) or not (_live(state) or state.withheld):
                continue
            keep()
            if state.withheld:
                for m in repo.members_of(conn, uid):
                    repo.set_withheld(conn, m["member_uid"], state.withheld)
                conn.commit()
            if _end(conn, target, report, uid, now=now, secrets=secrets, fetcher=fetcher):
                report.ended += 1
    if report.errors:
        return report   # a companion still holding a live event stays enabled for a rerun
    for companion in repo.companions(conn):
        repo.set_enabled(conn, companion.id, False)
    conn.commit()
    return report


def publish_safely(conn, target, **kwargs) -> MergeReport:
    """`publish` for a route whose own change is already saved: a crash is reported."""
    try:
        return publish(conn, target, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the person's change stands either way
        return MergeReport(errors=[f"merge step failed: {exc}"])
