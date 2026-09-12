"""Taking one event off the calendar, and keeping it off.

Deleting an event in Calendar.app does not work, and the way it fails is quiet.
The mirror keeps no state file — identity lives in the events themselves
(`mac/README.md`) — so a deleted event is indistinguishable from one that was
never written, and the next run creates it again. Deleting it on a phone is no
better: the phone is subscribed to Radicale, and Radicale still has it.

The decision has to live here, for two reasons. It has to survive the next poll,
because the feed goes on publishing the event forever — this is not a deletion,
it is a standing instruction. And it has to reach *everyone*: the grandparents'
subscription reads the same collection the family's phones do, so an exclusion
kept on one Mac would leave the event on every other device.

Two reasons, one mechanism. The event comes off the calendar either way, and
which reason it was changes nothing but the label:

- **not_attending** — the game is on, this kid is not going to it.
- **cancelled** — the game is off, and the feed does not say so. That is not a
  hypothetical: Player360 exports a cancelled practice as an ordinary one
  indefinitely, and `upstream.py` can see that *something* was edited without
  being able to say what (docs/sources/player360.md, Trap 2). calsync will not
  infer a delete from that. A person can state it, which is what this is.

**Only a person reaches this.** `upstream.py` still notifies and changes no
calendar, and nothing in the poll path sets the flag — the same division the
review gate draws (docs/API.md): an agent may put something in front of you and
has no path to the function that writes it.

What it is *not* is a second writer of events. `enforce` cancels, the same call
`retire.py` makes, and putting an event back is an ordinary sync: clearing the
flag leaves a cancelled `event_state` row, `known_hashes` skips cancelled rows,
so the next poll sees the event as new and writes it. One thing creates events,
and it is `sync.py`.

A warm-up follows its game (`warmup.py`), here as everywhere else. It is a
shadow of the fixture, so withholding a game withholds the warm-up in front of
it — without that you keep the 45 minutes of getting there for a game nobody is
going to. The flag is stored against the game alone and the warm-up is derived
from it, because two rows meaning one decision is one row that can be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import repo, warmup
from .targets import TargetError, TargetRef

#: Why an event is off the calendar, and what to call it in front of a person.
#: The key is what the column holds; nothing else may be stored there.
REASONS: dict[str, str] = {
    "not_attending": "not attending",
    "cancelled": "cancelled",
}


@dataclass
class WithholdReport:
    removed: int = 0
    #: Withheld and already off the calendar. The normal case on every poll
    #: after the first, and not worth a word to anybody.
    already_gone: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def is_withheld(uid: str, withheld: set[str]) -> bool:
    """Is this uid off the calendar by somebody's decision?

    True for a warm-up whose game is withheld, and this is the only place that
    derivation lives: both callers that decide whether to write an event — the
    sync loop's filter and `enforce` — ask it here rather than each deriving the
    parent themselves.
    """
    if uid in withheld:
        return True
    parent = warmup.parent_of(uid)
    return parent is not None and parent in withheld


def enforce(conn, source_id: str, target) -> WithholdReport:
    """Take every withheld event of this source off the calendar.

    Idempotent, and deliberately so: it is called both from the console the
    moment somebody presses the button and from every sync afterwards. The
    button is what makes it feel immediate; the sync is what makes it true when
    the calendar server was unreachable at the moment it was pressed.

    Borrows the sync loop's ordering rule without exception — **cancel at the
    target first, then record it**. The other order marks an event gone while it
    is still on somebody's phone, and nothing ever retries it.
    """
    report = WithholdReport()
    states = repo.event_states(conn, source_id)
    withheld = {uid for uid, state in states.items() if state.withheld}

    for uid, state in states.items():
        if not is_withheld(uid, withheld):
            continue
        if state.cancelled:
            report.already_gone += 1
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
            # Keep going, like `retire.py`: one unreachable event should not
            # strand the others, and the flag stays set so the next sync retries.
            report.errors.append(f"{uid}: {exc}")
            continue
        repo.mark_event_cancelled(conn, uid)
        report.removed += 1

    return report
