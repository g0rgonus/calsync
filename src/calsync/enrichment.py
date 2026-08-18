"""Telling you that something is waiting to be answered.

`/review` is only worth having if you know to look at it. A queue nobody visits
is the same as no queue — the events sit in the enrichment calendar, off the
family's phones, which is *safe* but not the point. The point is that they get
onto the right calendar quickly, and that needs a nudge.

**Once per question, not once per poll.** The poller runs every twenty minutes
and the events wait until somebody acts, so the naive version pushes the same
notification seventy times a day and is muted by lunchtime. What is recorded is
a fingerprint of *the questions themselves*: more events arriving against a
question already announced is not news, a genuinely new question is. This is the
`dormancy_notified` pattern (`seasonend.py`), for the same reason.

**Reset when the queue clears**, so the next occurrence is announced again. A
flag that latches forever would make this a once-in-a-lifetime notification.

Only questions that actually *hold* an event count. An unresolved venue is a
real gap and appears on the source page, but it does not keep anything off the
calendar, so paging somebody about it would train them to ignore the one signal
that means events are waiting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import matrix, notify, repo
from .inspection import name_candidates
from .models import BLOCKING_DIAGNOSTICS
from .routing import slugify
from .settings import Settings
from .sync import SyncReport

#: Everything an agent could answer, which is deliberately *wider* than what
#: holds an event back. A venue nobody has entered does not stop a fixture
#: reaching the calendar, so it never pages a human — but resolving it is the
#: single best use of a model this project has (CLAUDE.md: a model is the last
#: tier of venue resolution, never the first), so it is worth asking about.
#:
#: Held-ness and ask-ability are separate questions and conflating them would
#: either page somebody about a map pin or never ask about one.
DISPATCHABLE = BLOCKING_DIAGNOSTICS + ("unresolved_venues",)

#: The task vocabulary from docs/MATRIX.md §2, mapped to the diagnostics that
#: produce them. Reused rather than reinvented so the room's contract and the
#: document stay the same thing.
TASK_TYPES = {
    "unidentified": "resolve_activity",
    "unknown_types": "classify_kind",
    "unknown_categories": "classify_kind",
    "unresolved_venues": "normalize_venue",
}

QUESTIONS = {
    "unidentified": "which of these names is our team?",
    "unknown_types": "is this label a game or a practice?",
    "unknown_categories": "is this category a game or a practice?",
    "unresolved_venues": "where is this, and is it another name for a place we know?",
}


@dataclass
class Outcome:
    source_id: str
    #: Events actually sitting in the enrichment collection right now.
    held: int = 0
    notified: bool = False
    errors: list[str] = field(default_factory=list)


#: Kinds where every unrecognised string is the *same* question with one
#: answer. Ten fixtures naming two teams each is not ten questions — it is one
#: ("which of these names is ours?") whose single answer, an activity alias,
#: resolves all ten. `web/gate.py` already collapses it that way for a human,
#: and asking an agent ten times would cost ten round trips and ten chances to
#: answer inconsistently.
#:
#: Everything else stays per-item, because it genuinely is: whether "Skills
#: Session" is a game says nothing about "Playoff Game2", and one venue's
#: address says nothing about another's.
COLLAPSED = ("unidentified",)


@dataclass(frozen=True)
class Task:
    """One question, addressable.

    ``id`` is derived from the question rather than allocated, so the same
    unanswered question is the same task on every poll. That is what lets the
    Matrix transaction id dedupe a re-post, and what will let an answer be
    accepted against a task nobody had to remember issuing.
    """

    id: str
    type: str
    kind: str
    question: str
    #: What the coach actually typed. More than one only where a single answer
    #: covers all of them — see COLLAPSED.
    context: tuple[str, ...]
    source_id: str
    #: Answers worth proposing, best first. Same ranking the console offers a
    #: human, so the two are choosing from one list rather than two.
    candidates: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        out = {
            "task_id": self.id,
            "type": self.type,
            "question": self.question,
            "context": list(self.context),
        }
        if self.candidates:
            out["candidates"] = list(self.candidates)
        return out


def _task_id(source_id: str, kind: str, items: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for part in (source_id, kind, *items):
        digest.update(part.encode())
        digest.update(b"\x1e")
    return f"task_{digest.hexdigest()[:16]}"


def tasks(source_id: str, report: SyncReport) -> tuple[Task, ...]:
    """Every question an agent could answer, addressable and stable."""
    out: list[Task] = []
    for kind in DISPATCHABLE:
        items = tuple(sorted(set(report.diagnostics.get(kind, ()))))
        if not items:
            continue
        groups = [items] if kind in COLLAPSED else [(item,) for item in items]
        for group in groups:
            candidates: tuple[str, ...] = ()
            if kind == "unidentified":
                candidates = tuple(
                    c.token for c in name_candidates(list(group))[:6]
                )
            out.append(
                Task(
                    id=_task_id(source_id, kind, group),
                    type=TASK_TYPES[kind],
                    kind=kind,
                    question=QUESTIONS[kind],
                    context=group,
                    source_id=source_id,
                    candidates=candidates,
                )
            )
    return tuple(out)


def questions(report: SyncReport) -> tuple[str, ...]:
    """The unanswered questions that are holding events back, verbatim.

    Deliberately not every diagnostic. `unresolved_venues` is left out because
    it does not hold anything — see the module docstring.
    """
    found: list[str] = []
    for kind in BLOCKING_DIAGNOSTICS:
        found.extend(report.diagnostics.get(kind, ()))
    return tuple(sorted(set(found)))


def _fingerprint(items: tuple[str, ...]) -> str:
    """A short, stable id for a set of questions.

    Hashed rather than stored verbatim: a season's worth of unrecognised
    fixtures is a long string, and this column exists to be compared, not read.
    """
    digest = hashlib.sha256()
    for item in items:
        digest.update(item.encode())
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def _message(activity_name: str, held: int, items: tuple[str, ...]) -> tuple[str, str]:
    noun = "event" if held == 1 else "events"
    title = f"{activity_name}: {held} {noun} waiting"
    lines = [
        f"calsync could not tell which calendar {'it' if held == 1 else 'they'} "
        "belong in, so nothing has gone to the family's calendars.",
        "",
    ]
    # Bounded: a notification is a lock screen, not a report. The page has them
    # all, and that is what the link is for.
    lines += [f"· {item}" for item in items[:5]]
    if len(items) > 5:
        lines.append(f"· and {len(items) - 5} more")
    return title, "\n".join(lines)


def review(
    conn,
    source: repo.Source,
    report: SyncReport,
    *,
    secrets,
    base_url: str = "",
    sender=notify.send,
) -> Outcome:
    """Announce a source's outstanding questions, at most once each.

    Safe to call after every poll: it does nothing unless something is actually
    held *and* the questions differ from whatever was last announced.
    """
    settings = Settings.load(conn)
    collection = (
        slugify(settings.enrichment_collection) if settings.enrichment_collection else ""
    )
    held = repo.events_in_collection(conn, source.id, collection)
    outcome = Outcome(source_id=source.id, held=held)

    row = conn.execute(
        "SELECT review_notified FROM sources WHERE id = ?", (source.id,)
    ).fetchone()
    already = (row["review_notified"] if row else None) or ""

    if not held:
        # Cleared. Forget what was announced so a later occurrence is news
        # again, rather than being swallowed by a flag that never resets.
        if already:
            conn.execute(
                "UPDATE sources SET review_notified = NULL WHERE id = ?", (source.id,)
            )
            conn.commit()
        return outcome

    items = questions(report)
    if not items:
        # Held events but nothing to ask: the feed now parses cleanly and the
        # next poll will release them. Not worth a notification.
        return outcome

    signature = _fingerprint(items)
    if signature == already:
        return outcome

    config = notify.load(conn)
    if config.available(secrets):
        activity = repo.get_activity(conn, source.activity_id)
        title, message = _message(activity.name, held, items)
        try:
            sender(
                config, secrets, message, title=title,
                url=f"{base_url}/review" if base_url else None,
                url_title="Answer these",
            )
            outcome.notified = True
        except notify.NotifyError as exc:
            # Not fatal and not retried: the condition is still true at the next
            # poll, and the console shows it whether or not the push arrived.
            outcome.errors.append(str(exc))

    # Recorded even when no push went out, so a deployment with no Pushover
    # configured does not re-evaluate this on every poll forever.
    conn.execute(
        "UPDATE sources SET review_notified = ? WHERE id = ?", (signature, source.id)
    )
    conn.commit()
    return outcome


# --- telling the agent ------------------------------------------------------
#
# Separate from `review` above, and tracked on its own column, because the two
# announcements have different audiences and different failure modes. A push
# that never reaches you is an annoyance; a task that never reaches Hermes means
# the work simply does not happen. Sharing one flag would also mean that
# configuring Matrix after a queue had opened silently skipped it.


def _dispatch_body(activity_name: str, held: int, items: tuple[Task, ...]) -> str:
    """One message per source per batch, never one per question.

    docs/MATRIX.md §2, "Keep the room readable": forty questions posted
    individually is a room nobody can read, and a room nobody reads is not an
    audit transcript.

    Prose *and* a JSON block. The prose is for the human members of the room —
    it is a transcript before it is a queue — and the block is so an agent never
    has to scrape prose that was written to be readable rather than parsed.
    """
    lines = [
        f"**{activity_name}** — {len(items)} open question(s)",
    ]
    if held:
        lines.append(
            f"{held} event(s) are waiting in the enrichment calendar and are not "
            "on anyone's calendar until these are answered."
        )
    lines.append("")
    for task in items:
        subject = task.context[0] if len(task.context) == 1 else (
            f"{len(task.context)} fixtures"
        )
        lines.append(f"- `{task.type}` · {subject} — {task.question}")
        if task.candidates:
            lines.append(f"    candidates: {' · '.join(task.candidates)}")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({
        "source": items[0].source_id,
        "activity": activity_name,
        "held": held,
        "tasks": [task.as_dict() for task in items],
        # Filled in when the answer endpoint exists. Explicitly null rather than
        # absent, so an agent can tell "not answerable yet" from "this message
        # forgot to say how" — and so the shape does not change when it lands.
        "respond_via": None,
    }, indent=2, sort_keys=True))
    lines.append("```")
    return "\n".join(lines)


def dispatch(
    conn,
    source: repo.Source,
    report: SyncReport,
    *,
    secrets,
    opener=None,
    sender=matrix.send,
) -> Outcome:
    """Post this source's open questions to the room, at most once each.

    Safe to call after every poll. Does nothing unless Matrix is configured and
    the question set differs from whatever was last posted.

    Deliberately wider than :func:`review`: a question that does not hold an
    event back is still worth asking, and a venue is the clearest example.
    """
    settings = Settings.load(conn)
    collection = (
        slugify(settings.enrichment_collection) if settings.enrichment_collection else ""
    )
    held = repo.events_in_collection(conn, source.id, collection)
    outcome = Outcome(source_id=source.id, held=held)

    row = conn.execute(
        "SELECT review_dispatched FROM sources WHERE id = ?", (source.id,)
    ).fetchone()
    already = (row["review_dispatched"] if row else None) or ""

    open_tasks = tasks(source.id, report)
    if not open_tasks:
        if already:
            conn.execute(
                "UPDATE sources SET review_dispatched = NULL WHERE id = ?",
                (source.id,),
            )
            conn.commit()
        return outcome

    signature = _fingerprint(tuple(task.id for task in open_tasks))
    if signature == already:
        return outcome

    config = matrix.load(conn)
    if not config.configured or not config.room_id:
        # Not configured is not a failure, and recording nothing means the
        # questions go out whenever somebody does configure it.
        return outcome

    activity = repo.get_activity(conn, source.activity_id)
    try:
        sender(
            config, secrets, _dispatch_body(activity.name, held, open_tasks),
            # Derived from what the message is about, so a retry after a timeout
            # updates the same message rather than posting the questions twice.
            transaction_id=f"calsync-tasks-{source.id}-{signature}",
            **({"opener": opener} if opener is not None else {}),
        )
        outcome.notified = True
    except matrix.MatrixError as exc:
        # Not fatal and not retried here: the condition is still true next poll.
        outcome.errors.append(str(exc))
        return outcome

    conn.execute(
        "UPDATE sources SET review_dispatched = ? WHERE id = ?", (signature, source.id)
    )
    conn.commit()
    return outcome
