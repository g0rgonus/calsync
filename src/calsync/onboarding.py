"""Turn a confirmed inspection into rows, without ever storing a credential.

Everything here sits between :mod:`calsync.inspection` (which reads a feed and
writes nothing) and :func:`calsync.config.apply` (which writes rows and reads no
feeds). The web layer is thin over both.

The part worth reading is :func:`templatise`. A feed URL handed over by a team
app is a bearer capability — whoever holds it can read a child's schedule and
the physical locations they will be at, with timestamps. It cannot go in the
database, because ``sources.url_template`` is meant to stay safe to read,
export, back up and paste into a bug report. So onboarding splits the credential
out into the secret store and leaves a ``{{secret:ref}}`` placeholder behind,
and it *proves* it did so correctly by reassembling the template and comparing
against the URL it was given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse, urlunparse

from . import config as config_mod
from . import repo
from .fetch import FetchError, render_url
from .routing import slugify
from .secrets import SecretError, SecretStore

#: Query parameter names that carry a credential. Matched loosely on purpose:
#: over-vaulting a harmless parameter costs nothing, and under-vaulting one puts
#: a bearer token in the database.
SECRETISH = re.compile(r"token|key|auth|secret|access|sig|pass|cred|ticket", re.IGNORECASE)

#: Below this length a path segment is a word, not a credential.
OPAQUE_MIN = 12

#: Short prefixes so a source id reads like ``tr-hawks`` rather than
#: ``teamreach-hawks-spring-2026``. Anything unlisted uses its own kind.
_KIND_PREFIX = {"teamreach": "tr", "player360": "p360"}

_WORDY = re.compile(r"^[a-z][a-z-]*$", re.IGNORECASE)

#: The placeholder syntax ``fetch.render_url`` expands. Matched here only to
#: take placeholders back out before checking a template for a leaked value.
_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")


class OnboardingError(RuntimeError):
    """The source could not be created. Nothing is written when this is raised."""


@dataclass(frozen=True)
class UrlPart:
    """One piece of a URL the operator can choose to vault."""

    #: ``query:token`` or ``path`` — an opaque handle the form posts back.
    key: str
    label: str
    #: Never the value. A credential must not reach a page, even one served on
    #: loopback: it would land in the browser's history, cache and scrollback.
    preview: str
    suspect: bool


@dataclass(frozen=True)
class UrlPlan:
    """What of this URL looks like a credential, and what to do by default."""

    parts: tuple[UrlPart, ...] = ()
    #: ``params`` | ``path`` | ``none``
    recommended: str = "none"
    recommended_keys: tuple[str, ...] = ()
    reason: str = ""


def _redact(value: str) -> str:
    """Enough to recognise it, not enough to use it."""
    if len(value) <= 6:
        return "•" * len(value)
    return f"{value[:2]}{'•' * 6}{value[-2:]}"


def _query_pairs(query: str) -> list[tuple[str, str, str]]:
    """Split a raw query without decoding it.

    The original chunk comes back alongside the split, because anything not
    being vaulted has to be reassembled character for character — ``a=`` and
    ``a`` are different query strings, and a round-trip that loses the ``=``
    would fail the verification for no reason.
    """
    pairs = []
    for chunk in query.split("&"):
        if not chunk:
            continue
        name, sep, value = chunk.partition("=")
        pairs.append((name, value if sep else "", chunk))
    return pairs


def _opaque(segment: str) -> bool:
    """Does this path segment look like a handle rather than a word?"""
    stem = segment.rsplit(".", 1)[0] if "." in segment else segment
    if len(stem) < OPAQUE_MIN:
        return False
    return not _WORDY.match(stem)


def analyse_url(url: str) -> UrlPlan:
    """Find the parts of a URL that should go to the secret store.

    Recommends, never decides. A capability URL with no query string at all —
    which is the shape a team app usually hands out — has its final path segment
    proposed, because that segment *is* the credential.
    """
    parsed = urlparse(url.strip())
    parts: list[UrlPart] = []

    suspects: list[str] = []
    for name, value, _chunk in _query_pairs(parsed.query):
        suspect = bool(SECRETISH.search(name)) and bool(value)
        key = f"query:{name}"
        parts.append(
            UrlPart(
                key=key,
                label=name,
                preview=_redact(unquote(value)),
                suspect=suspect,
            )
        )
        if suspect:
            suspects.append(key)

    segments = [s for s in parsed.path.split("/") if s]
    tail = segments[-1] if segments else ""
    if tail:
        parts.append(
            UrlPart(
                key="path",
                label=f"last path segment ({tail.rsplit('.', 1)[-1] if '.' in tail else 'no extension'})",
                preview=_redact(tail),
                suspect=_opaque(tail) and not suspects,
            )
        )

    if suspects:
        return UrlPlan(
            parts=tuple(parts),
            recommended="params",
            recommended_keys=tuple(suspects),
            reason="these parameters are named like credentials",
        )
    if tail and _opaque(tail):
        return UrlPlan(
            parts=tuple(parts),
            recommended="path",
            recommended_keys=("path",),
            reason="the URL has no query string, so the path is the credential",
        )
    return UrlPlan(
        parts=tuple(parts),
        recommended="none",
        reason="nothing in this URL reads as a credential",
    )


def templatise(url: str, keys, ref: str) -> tuple[str, dict[str, str]]:
    """Replace the chosen parts with ``{{secret:...}}`` placeholders.

    Returns the template to store and the secrets to write. The values are
    *decoded* before storing, because :func:`~calsync.fetch.render_url`
    percent-encodes whatever it substitutes — storing the encoded form would
    double-encode it on every fetch.

    The template is reassembled and compared against the original before being
    returned. A URL that will not round-trip is refused rather than saved: a
    silently mangled feed URL surfaces days later as a source that has simply
    stopped working.
    """
    keys = set(keys or ())
    if not keys:
        return url.strip(), {}

    parsed = urlparse(url.strip())
    secrets: dict[str, str] = {}

    rebuilt: list[str] = []
    for name, value, chunk in _query_pairs(parsed.query):
        if f"query:{name}" in keys and value:
            placeholder_ref = f"{ref}_{slugify(name).replace('-', '_')}"
            secrets[placeholder_ref] = unquote(value)
            rebuilt.append(f"{name}={{{{secret:{placeholder_ref}}}}}")
        else:
            rebuilt.append(chunk)
    query = "&".join(rebuilt)

    path = parsed.path
    if "path" in keys:
        segments = path.split("/")
        index = max(i for i, s in enumerate(segments) if s)
        tail = segments[index]
        stem, dot, extension = tail.rpartition(".")
        if not dot:
            stem, extension = tail, ""
        secrets[ref] = unquote(stem)
        segments[index] = f"{{{{secret:{ref}}}}}{dot}{extension}"
        path = "/".join(segments)

    template = urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, query, parsed.fragment)
    )

    _verify_round_trip(template, secrets, url.strip())
    return template, secrets


class _StubStore:
    """Resolves only the secrets about to be written. Never touches disk."""

    def __init__(self, values: dict[str, str]):
        self._values = values

    def get(self, ref: str) -> str:
        try:
            return self._values[ref]
        except KeyError:
            raise SecretError(f"no secret for {ref!r}") from None


def _verify_round_trip(template: str, secrets: dict[str, str], original: str) -> None:
    try:
        assembled = render_url(
            template, secrets=_StubStore(secrets), now=datetime.now(timezone.utc)
        )
    except (FetchError, SecretError) as exc:
        raise OnboardingError(f"the URL could not be templated safely: {exc}") from exc

    if assembled.url != "".join(original.split()):
        raise OnboardingError(
            "splitting the credential out of this URL would change it, so it was "
            "not saved. Store it verbatim, or add the secret by hand."
        )
    # Look at the template with its placeholders removed. The refs are derived
    # from the source id, which is derived from the team name — so a feed at
    # ``/ics/hawks`` vaulted under ``tr_hawks_spring_2026`` would otherwise
    # report its own placeholder as the leak.
    remainder = _PLACEHOLDER.sub("", template)
    if any(value and value in remainder for value in secrets.values()):
        raise OnboardingError("the credential is still present in the template")


# --- naming -----------------------------------------------------------------


def propose_ids(conn, *, child_id: str, sport: str, team_name: str, kind: str):
    """Readable, stable, unique ids. ``patrick-soccer-hawks`` / ``tr-hawks``."""
    team = slugify(team_name)
    activity = _unique(conn, "activities", f"{slugify(child_id)}-{slugify(sport)}-{team}")
    prefix = _KIND_PREFIX.get(kind, slugify(kind))
    source = _unique(conn, "sources", f"{prefix}-{team}")
    return activity, source


def _unique(conn, table: str, base: str) -> str:
    if not repo.id_taken(conn, table, base):
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if not repo.id_taken(conn, table, candidate):
            return candidate
    raise OnboardingError(f"could not find a free id starting {base!r}")


# --- creating ---------------------------------------------------------------


@dataclass
class Draft:
    """Everything needed to create one team. Three fields are the operator's."""

    url: str
    child_id: str
    sport: str
    team_name: str
    kind: str
    tz: str
    #: The string that means *us* in fixture summaries. Becomes an activity
    #: alias, which is what resolves "Hawks vs Strikers" into an opponent and a
    #: home/away flag.
    token: str | None = None
    season_start: str | None = None
    season_end: str | None = None
    poll_interval_s: int = 1200
    secret_keys: tuple[str, ...] = ()
    staging_collection: str = "onboarding"
    #: Carried forward from last season where there is one.
    emoji: str | None = None
    league: str | None = None
    age_group: str | None = None
    alarm_game_min: int = 90
    alarm_practice_min: int = 30
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Created:
    activity_id: str
    source_id: str
    secret_refs: tuple[str, ...] = ()
    log: tuple[str, ...] = field(default=())


def clone_forward(conn, child_id: str, sport: str) -> "Draft | None":
    """Prefill from this child's last team in this sport, if there was one.

    Only the things that survive a season change are carried: the league, the
    age group, the timezone, the alarm policy. Never the name, never the feed,
    never the aliases — those are exactly what churns.
    """
    previous = repo.previous_season(conn, child_id, sport)
    if previous is None:
        return None
    return Draft(
        url="",
        child_id=child_id,
        sport=sport,
        team_name="",
        kind="",
        tz=previous.tz,
        emoji=previous.emoji,
        league=previous.league,
        age_group=previous.age_group,
        alarm_game_min=previous.alarm_game_min,
        alarm_practice_min=previous.alarm_practice_min,
    )


def create(conn, draft: Draft, *, secrets: SecretStore) -> Created:
    """Create the activity and the source, staged to the onboarding calendar.

    Ordering matters. The credential is written first: a source row pointing at
    a secret that does not exist is a source that fails on every poll, and the
    operator would have to work out why from a fetch error. If the secret store
    is not writable the whole thing is refused, and nothing is created.
    """
    if not draft.team_name.strip():
        raise OnboardingError("the team needs a name")
    if not draft.url.strip():
        raise OnboardingError("the feed URL is missing")

    activity_id, source_id = propose_ids(
        conn,
        child_id=draft.child_id,
        sport=draft.sport,
        team_name=draft.team_name,
        kind=draft.kind,
    )

    template, pending = templatise(draft.url, draft.secret_keys, source_id.replace("-", "_"))
    for ref, value in pending.items():
        secrets.put(ref, value)

    aliases = {a.strip() for a in draft.aliases if a and a.strip()}
    if draft.token:
        aliases.add(draft.token.strip())
    aliases.discard(draft.team_name.strip())

    log = config_mod.apply(
        conn,
        {
            "activities": [
                {
                    "id": activity_id,
                    "child": draft.child_id,
                    "name": draft.team_name.strip(),
                    "sport": draft.sport,
                    "emoji": draft.emoji,
                    "league": draft.league,
                    "age_group": draft.age_group,
                    "tz": draft.tz,
                    "aliases": sorted(aliases),
                    "season": {"start": draft.season_start, "end": draft.season_end},
                    "alarm_policy": {
                        "game": f"{draft.alarm_game_min}m",
                        "practice": f"{draft.alarm_practice_min}m",
                    },
                }
            ],
            "sources": [
                {
                    "id": source_id,
                    "activity": activity_id,
                    "kind": draft.kind,
                    "shape": "feed",
                    "poll_interval": f"{draft.poll_interval_s}s",
                    "url_template": template,
                    # Only set when there is one ref; several become
                    # `<ref>_<param>` and the template names them all.
                    "secret_ref": next(iter(pending)) if len(pending) == 1 else None,
                    "enabled": 1,
                }
            ],
        },
    )

    # Staged, not live. A new feed goes to the onboarding calendar until its
    # fixtures appear and parse (docs/ONBOARDING.md §5) — which may be weeks.
    repo.set_staging(conn, source_id, draft.staging_collection or None)

    return Created(
        activity_id=activity_id,
        source_id=source_id,
        secret_refs=tuple(sorted(pending)),
        log=tuple(log),
    )
