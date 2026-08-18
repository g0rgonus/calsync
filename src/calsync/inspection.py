"""Read a feed before it has an activity, a source, or a row anywhere.

Onboarding starts with a URL and nothing else, so everything the operator would
otherwise have to type has to come out of the feed itself (docs/ONBOARDING.md
§2). This module is that step, and it is deliberately **pure and side-effect
free**: it takes bytes and returns derivations. Nothing here writes to the
database, because the whole point is to see what a feed contains *before*
deciding whether to create anything.

The derivations, measured against the four feeds in ``tests/fixtures``:

    team name       X-WR-CALNAME, which every TeamReach feed sets and
                    Player360's does not (it publishes a generic product name)
    season bounds   min/max DTSTART
    team token      frequency — in a "us vs them" feed our own name is on every
                    fixture and each opponent appears once or twice
    venues          LOCATION, or the tail of the SUMMARY when there is no
                    LOCATION, with the field designator split off
    counts          how the feed reads before any configuration exists

**The regexes come from the adapters rather than being restated here.** A
private import is the lesser evil: a second copy would drift, and drift in this
direction is worse than ugly — the UI would propose a token that the parser then
declines to use, and the operator would have no way to tell why.

Nothing in here guesses. A token is proposed only when the frequency evidence is
decisive, and the counts are labelled as a reading of the feed rather than as
the parse, because the real parse needs an activity and happens later.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from icalendar import Calendar

from .normalize import venue as venue_norm
from .sources import player360, teamreach

#: How many distinct SUMMARY strings to carry back as evidence. Enough to see
#: which convention a coach is using; not so many that the page becomes the feed.
SAMPLE_LIMIT = 12

#: PRODID fragments that name an adapter. Detection is a convenience — the
#: operator can always override it — so an unknown feed yields None rather than
#: a guess that would parse the wrong way round.
_PRODID_HINTS: tuple[tuple[str, str], ...] = (
    ("teamreach", "teamreach"),
    ("360 player", "player360"),
    ("360player", "player360"),
)

_WS = re.compile(r"\s+")


class InspectionError(RuntimeError):
    """The feed could not be read well enough to onboard from it."""


@dataclass(frozen=True)
class TokenCandidate:
    """A team name seen on one side of a fixture, and how often."""

    token: str
    count: int


@dataclass(frozen=True)
class VenueCandidate:
    """A place named in the feed, with the field designator already split off.

    ``fields`` collects the designators seen at this venue ("#2", "Field 3").
    They are kept separate because venue identity excludes them — one park, one
    pin, however many fields.
    """

    name: str
    count: int
    fields: tuple[str, ...] = ()
    address: str | None = None
    #: Filled in by the caller that has a database. Inspection itself never
    #: touches ``venue_aliases``.
    known: bool = False


@dataclass(frozen=True)
class FeedInspection:
    """Everything one feed says about itself."""

    event_count: int
    kind: str | None = None
    calendar_name: str | None = None
    prodid: str | None = None
    first_start: datetime | None = None
    last_start: datetime | None = None
    tokens: tuple[TokenCandidate, ...] = ()
    venues: tuple[VenueCandidate, ...] = ()
    summaries: tuple[tuple[str, int], ...] = ()
    #: How the feed reads with no configuration at all. An estimate, and named
    #: so: the real classification needs an activity and its known tokens, and
    #: happens in the preview.
    reads_as_games: int = 0
    reads_as_practices: int = 0
    reads_as_unclear: int = 0
    raw_sha256: str = ""
    warnings: tuple[str, ...] = field(default=())

    @property
    def team_token(self) -> str | None:
        """The proposed name for *our* team, or None if the feed can't say.

        Decisive means: seen at least twice, and at least twice as often as the
        runner-up. A feed of "Game vs Cougars" fixtures yields opponents at one
        appearance each and correctly proposes nothing — proposing the first
        opponent as our own team would be worse than proposing nothing, because
        it looks like an answer.
        """
        if not self.tokens:
            return None
        top = self.tokens[0]
        if top.count < 2:
            return None
        runner_up = self.tokens[1].count if len(self.tokens) > 1 else 0
        return top.token if top.count >= 2 * runner_up else None

    @property
    def team_name(self) -> str | None:
        """What to prefill the team name with.

        The calendar name is the better label — "Otters Spring 2026" is what the
        coach called it — and the frequency token is the better *alias*, since
        that is the string that actually appears in fixtures.
        """
        return self.calendar_name or self.team_token

    @property
    def season_start(self) -> str | None:
        return self.first_start.date().isoformat() if self.first_start else None

    @property
    def season_end(self) -> str | None:
        return self.last_start.date().isoformat() if self.last_start else None

    @property
    def has_fixtures(self) -> bool:
        """Any games at all? A practices-only feed is normal and not a problem.

        Coaches publish practices first, so this being False at onboarding is
        expected — it is the reason a source stays staged (docs/ONBOARDING.md §5).
        """
        return self.reads_as_games > 0


def _text(component, key: str) -> str | None:
    value = component.get(key)
    if value is None:
        return None
    return _WS.sub(" ", str(value)).strip() or None


def _calendar_text(cal, *keys: str) -> str | None:
    for key in keys:
        value = cal.get(key)
        if value is not None and str(value).strip():
            return _WS.sub(" ", str(value)).strip()
    return None


def detect_kind(prodid: str | None) -> str | None:
    """Which adapter a PRODID points at, or None to let the operator choose."""
    if not prodid:
        return None
    needle = prodid.casefold()
    for fragment, kind in _PRODID_HINTS:
        if fragment in needle:
            return kind
    return None


def _start(component) -> datetime | None:
    prop = component.get("DTSTART")
    if prop is None:
        return None
    value = getattr(prop, "dt", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        # Inspection is a read, so a naive value is reported rather than raised
        # on: the operator needs to see the feed to decide anything about it.
        # The adapter will refuse it at parse time, which is where refusing
        # belongs.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def fixture_sides(summary: str) -> tuple[str, str] | None:
    """The two team names in a fixture summary, or None if it isn't one.

    A summary whose left side is nothing but a type word ("Game vs Cougars")
    returns None: it names an opponent but says nothing about who we are, so it
    is not evidence either way.
    """
    text = _WS.sub(" ", (summary or "").strip())
    parts = teamreach._VERSUS.split(text, maxsplit=1)
    if len(parts) != 2:
        return None

    left, right = parts[0].strip(), parts[1].strip()
    # A venue can be tacked on after the opponent — "Otters vs Meteors - Windmere".
    if "-" in right:
        right = right.partition("-")[0].strip()

    prefix = teamreach._TYPE_PREFIX.match(left)
    if prefix:
        left = left[prefix.end():].strip()

    if not left or not right:
        return None
    return left, right


def name_candidates(summaries: list[str]) -> tuple[TokenCandidate, ...]:
    counter: Counter[str] = Counter()
    for summary in summaries:
        sides = fixture_sides(summary)
        if sides is None:
            continue
        counter.update(sides)
    # Ties broken alphabetically so the same feed always proposes the same
    # token; an unstable proposal would be indistinguishable from a real change.
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(TokenCandidate(token=t, count=n) for t, n in ordered)


def _venue_text(component) -> str | None:
    """LOCATION when the coach filled it in, else the tail of the SUMMARY.

    Same precedence the TeamReach adapter uses, for the same reason: LOCATION is
    a field somebody chose to populate, where the summary tail is whatever was
    left over.
    """
    location = _text(component, "LOCATION")
    if location:
        return location
    parsed = teamreach.parse_summary(_text(component, "SUMMARY") or "")
    return parsed.venue_text


def _venues(components) -> tuple[VenueCandidate, ...]:
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    fields: dict[str, list[str]] = {}
    addresses: dict[str, str] = {}

    for component in components:
        raw = _venue_text(component)
        if not raw:
            continue
        cleaned = teamreach.clean_venue(raw)
        if not cleaned:
            continue
        # Peel a street address off first ("Thistledown Park 1009 Thistledown Rd"),
        # then the field designator off the name that remains.
        parsed = venue_norm.parse(cleaned)
        if parsed is None:
            continue
        name, designator = venue_norm.split_field(parsed.name or cleaned)
        if not name:
            continue

        key = name.casefold()
        counts[key] += 1
        display.setdefault(key, name)
        if parsed.address and key not in addresses:
            addresses[key] = parsed.address
        if designator and designator not in fields.setdefault(key, []):
            fields[key].append(designator)

    return tuple(
        VenueCandidate(
            name=display[key],
            count=count,
            fields=tuple(sorted(fields.get(key, []))),
            address=addresses.get(key),
        )
        for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def _reads_as(component) -> bool | None:
    """Game, practice, or undeterminable — using only what the feed carries.

    CATEGORIES wins where a feed publishes them, which is Player360's signal.
    Otherwise this falls back to the same summary heuristic the TeamReach
    adapter uses, minus the token matching that needs an activity.
    """
    categories = player360._categories(component)
    if categories:
        if any(c in player360.GAME_CATEGORIES for c in categories):
            return True
        if any(c in ("practice", "training") for c in categories):
            return False
        return None

    parsed = teamreach.parse_summary(_text(component, "SUMMARY") or "")
    # ``unidentified`` counts as a fixture here where it would not in the
    # adapter. It means "this is an X vs Y summary and neither side is us" —
    # which, with no activity and therefore no tokens, is every fixture in a
    # "us vs them" feed. The adapter declines to name an opponent it cannot
    # place, and is right to; inspection only needs to know a fixture is there.
    looks_like_a_fixture = bool(parsed.opponent) or parsed.unidentified
    return teamreach.classify(parsed.event_type, has_opponent=looks_like_a_fixture)


def inspect_feed(data: bytes | str) -> FeedInspection:
    """Read a feed body into the derivations onboarding needs.

    Raises :class:`InspectionError` rather than returning an empty result. A
    feed with no events is not something to onboard — and returning zeros here
    would put an empty inspection in front of the operator as though it were an
    answer.
    """
    raw_bytes = data.encode() if isinstance(data, str) else data
    if not raw_bytes.strip():
        raise InspectionError("the feed was empty")

    try:
        cal = Calendar.from_ical(raw_bytes)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a dead end
        raise InspectionError(f"this does not read as an iCalendar feed: {exc}") from exc

    components = [c for c in cal.walk() if c.name == "VEVENT"]
    if not components:
        raise InspectionError(
            "the feed parsed but contains no events, so there is nothing to onboard"
        )

    summaries = [_text(c, "SUMMARY") or "" for c in components]
    starts = [s for s in (_start(c) for c in components) if s is not None]

    warnings: list[str] = []
    if len(starts) != len(components):
        warnings.append(
            f"{len(components) - len(starts)} events have no usable start time"
        )
    if not any(_text(c, "LOCATION") for c in components):
        warnings.append("no event sets LOCATION; venues come from the summary text")

    readings = [_reads_as(c) for c in components]
    prodid = _calendar_text(cal, "PRODID")

    counted = Counter(s for s in summaries if s)
    return FeedInspection(
        event_count=len(components),
        kind=detect_kind(prodid),
        calendar_name=_calendar_text(cal, "X-WR-CALNAME", "NAME"),
        prodid=prodid,
        first_start=min(starts) if starts else None,
        last_start=max(starts) if starts else None,
        tokens=name_candidates(summaries),
        venues=_venues(components),
        summaries=tuple(
            sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))[:SAMPLE_LIMIT]
        ),
        reads_as_games=sum(1 for r in readings if r is True),
        reads_as_practices=sum(1 for r in readings if r is False),
        reads_as_unclear=sum(1 for r in readings if r is None),
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        warnings=tuple(warnings),
    )
