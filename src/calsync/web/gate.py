"""The promotion gate, presented as questions rather than as errors.

``SyncReport.promotable`` is a boolean, and a boolean is useless to the person
who has to make it true. The four conditions behind it (docs/ONBOARDING.md §5)
are each a specific question with a specific fix, and this module is what turns
one into the other:

    unidentified        which of these names is your team?      -> activity alias
    unknown types       what kind of event is this?             -> source vocabulary
    unresolved venues   where is this?                          -> venue row
    fixtures seen       (nothing — wait for the schedule)

The third state is the one that shapes the design. **A condition can be unmet
and fine.** Coaches publish practices first and add the game schedule weeks
later, so "no fixtures yet" is the expected resting state of a healthy new
source, not a failure to clear. It is rendered as waiting, never as a problem,
and the copy says so — otherwise the operator learns to ignore the one signal
that does mean something.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..inspection import name_candidates
from ..models import Activity
from ..sync import SyncReport

#: How a condition stands.
MET = "met"          # satisfied
ASKING = "asking"    # needs an answer only the operator can give
WAITING = "waiting"  # not satisfied, and nothing to do but wait
MOOT = "moot"        # the feed did not come back, so we cannot say


@dataclass(frozen=True)
class Condition:
    key: str
    #: Two words, because it sits under a 60px block on the dashboard.
    label: str
    state: str
    headline: str
    detail: str = ""
    #: The raw strings behind it, exactly as the coach typed them.
    items: tuple[str, ...] = ()
    #: Which answer form to offer: ``alias``, ``type``, ``venue``, or nothing.
    answer: str | None = None
    #: Names worth proposing as a one-click answer, most frequent first.
    suggestions: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.state in (ASKING, MOOT)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _team(report: SyncReport, activity: Activity) -> Condition:
    unidentified = tuple(report.diagnostics.get("unidentified", ()))
    if not unidentified:
        return Condition(
            key="team",
            label="Team matched",
            state=MET,
            headline=f"Every fixture named {activity.name} on one side.",
            detail="That is what fixes the opponent and the home/away marker.",
        )

    # Both sides of every unmatched fixture are candidates, and the frequency
    # trick that finds a team token at onboarding works here too: our own name
    # is on all of them, each opponent on one or two.
    #
    # Only repeats are offered. Nine unmatched fixtures name nine opponents once
    # each and us nine times, so listing every candidate buries the answer among
    # eight wrong ones — and each wrong one is a button that teaches the parser
    # an opponent's name is ours. Where nothing repeats there is no frequency
    # evidence at all, and a short list is honestly a list of guesses.
    ranked = name_candidates(list(unidentified))
    repeated = [c.token for c in ranked if c.count > 1]
    suggestions = repeated[:4] if repeated else [c.token for c in ranked[:3]]
    count = len(unidentified)
    return Condition(
        key="team",
        label="Team matched",
        state=ASKING,
        headline=(
            f"{count} {_plural(count, 'fixture', 'fixtures')} couldn't be matched "
            f"to {activity.name}."
        ),
        detail=(
            "The parser found an X vs Y summary and recognised neither side, so it "
            "named no opponent rather than guessing at one. Tell it which name is "
            "yours and every one of these resolves on the next poll — including the "
            "events already written, because the title is rendered from stored "
            "fields rather than saved."
        ),
        items=unidentified,
        answer="alias",
        suggestions=tuple(suggestions),
    )


def _types(report: SyncReport) -> Condition:
    items = tuple(
        report.diagnostics.get("unknown_types", ())
    ) + tuple(report.diagnostics.get("unknown_categories", ()))
    if not items:
        return Condition(
            key="types",
            label="Types known",
            state=MET,
            headline="Every event was classified as a game or a practice.",
        )
    count = len(items)
    return Condition(
        key="types",
        label="Types known",
        state=ASKING,
        headline=f"{count} event {_plural(count, 'label is', 'labels are')} unrecognised.",
        detail=(
            "These are filed as practices in the meantime, which is the safe "
            "default — a mis-filed practice is a smaller error than a missed "
            "game. Say which each one is and this source remembers it; the "
            "adapter's own vocabulary still covers everything else."
        ),
        items=items,
        answer="type",
    )


def _venues(report: SyncReport) -> Condition:
    items = tuple(report.diagnostics.get("unresolved_venues", ()))
    if not items:
        return Condition(
            key="venues",
            label="Venues placed",
            state=MET,
            headline="Every venue in the feed matched a known place.",
            detail="Venues outlast teams, so this stays true next season.",
        )
    count = len(items)
    return Condition(
        key="venues",
        label="Venues placed",
        state=ASKING,
        headline=f"{count} {_plural(count, 'place', 'places')} in this feed isn't known yet."
        if count == 1
        else f"{count} places in this feed aren't known yet.",
        detail=(
            "Events still carry the text, so the location is readable — it just is "
            "not a map pin. Name each place once and it is resolved for every team "
            "at that park, this season and every season after."
        ),
        items=items,
        answer="venue",
    )


def _games(report: SyncReport) -> Condition:
    if report.fixtures_seen > 0:
        return Condition(
            key="games",
            label="Games seen",
            state=MET,
            headline=(
                f"{report.fixtures_seen} "
                f"{_plural(report.fixtures_seen, 'game', 'games')} in the feed."
            ),
            detail="The opponent and home/away parsing has actually run.",
        )
    return Condition(
        key="games",
        label="Games seen",
        state=WAITING,
        headline="No games in this feed yet.",
        detail=(
            "Nothing to do. Coaches post the practice schedule first and add "
            "fixtures weeks later, so this is what a healthy new feed looks like "
            "in March. Until a game appears, the opponent parsing has never run "
            "and there is nothing to verify — the source stays on the onboarding "
            "calendar and this page will notice on its own."
        ),
    )


def conditions(report: SyncReport, activity: Activity) -> tuple[Condition, ...]:
    """The four gate conditions, in the order they tend to get answered."""
    if report.status == "error":
        # The feed did not come back, so nothing is known about the parse.
        # Showing three green blocks over a dead feed would be a lie.
        return tuple(
            Condition(
                key=key,
                label=label,
                state=MOOT,
                headline="Not checked — the feed did not come back.",
            )
            for key, label in (
                ("team", "Team matched"),
                ("types", "Types known"),
                ("venues", "Venues placed"),
                ("games", "Games seen"),
            )
        )
    return (_team(report, activity), _types(report), _venues(report), _games(report))


def summarise(conditions_: tuple[Condition, ...]) -> tuple[str, str]:
    """A card-level state and label: ``asking`` | ``waiting`` | ``ready``.

    Distinct from ``promotable`` on purpose. Promotable is about whether it is
    safe to move a source to the real calendars; this is about whether the
    operator has anything to do, and "waiting for the schedule" is a different
    answer from "answer these two questions".
    """
    asking = [c for c in conditions_ if c.state == ASKING]
    if any(c.state == MOOT for c in conditions_):
        return "down", "feed unreachable"
    if asking:
        count = len(asking)
        return "asking", f"{count} {_plural(count, 'question', 'questions')}"
    if any(c.state == WAITING for c in conditions_):
        return "waiting", "waiting for games"
    return "ready", "ready to promote"
