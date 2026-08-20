"""Routes. Thin on purpose — the work is in inspection, onboarding and gate."""

from __future__ import annotations

import calendar as calendar_mod
import json
import socketserver
import sqlite3
import threading
import urllib.request
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bottle
from bottle import Bottle, redirect, request, static_file, template

from .. import config as config_mod
from .. import db, dormancy, enrichment, matrix, notify, repo, retire, sources, targeting
from ..fetch import FetchError, http_fetch, render_url
from ..inspection import InspectionError, inspect_feed
from ..normalize import title as title_norm
from ..normalize import venue as venue_norm
from ..onboarding import (
    Draft,
    OnboardingError,
    analyse_url,
    clone_forward,
    create as create_source,
)
from ..routing import slugify
from ..secrets import SecretError, SecretStore
from ..settings import Settings, set_setting
from ..targets import TargetError
from ..sync import sync_source
from . import gate

HERE = Path(__file__).parent
VIEWS = HERE / "templates"
STATIC = HERE / "static"

#: How many feeds to check at once when the dashboard loads. The poller already
#: hits these every twenty minutes; a handful of extra GETs a few times a season
#: is not worth rate-limiting, but nor is opening thirty sockets at once.
CHECK_WORKERS = 4


class Refused(RuntimeError):
    """Shown to the operator as a message, not as a traceback."""


class _NoTarget:
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


def create_app(
    db_path,
    *,
    secrets: SecretStore | None = None,
    fetcher=http_fetch,
    clock=None,
    trusted_origins=(),
    matrix_opener=None,
    push_opener=None,
    write_target=None,
    calendar_transport=None,
) -> Bottle:
    """Build the console.

    ``fetcher`` is the one seam through which this app touches the network, used
    both by inspection and by the previews behind the gate. Keeping it a single
    parameter rather than an import is what lets the whole console be driven in
    a test without a socket — and the gate is exactly the part that most needs
    testing, since it decides what reaches a family's real calendar.

    ``clock`` pins "now" the way ``calsync sync --now`` does. It matters more
    here than it looks: the sync window is what decides which events are live,
    so a console tested against a fixture from last spring would report an empty
    parse and every gate condition would pass vacuously.

    ``write_target`` is the seam for the two console actions that reach the
    family's real calendars — retiring a source and syncing one on demand. One
    parameter rather than two, because a test that could inject a target for one
    of them and not the other would be testing a console nobody runs.
    """
    app = Bottle()
    secrets = secrets or SecretStore()
    clock = clock or _now
    trusted_origins = set(trusted_origins or ())
    # A second network seam, separate from `fetcher` because it speaks a
    # different protocol: feeds are GETs of iCalendar text, this is an
    # authenticated JSON API.
    matrix_opener = matrix_opener or urllib.request.urlopen
    push_opener = push_opener or urllib.request.urlopen
    db_path = str(db_path)

    # Once, at startup — not per request. Migration takes the write lock.
    with closing(db.open_db(db_path)):
        pass

    def connect():
        return closing(db.connect(db_path))

    # Which sources a "Sync now" is in flight for. The server is threaded, so
    # two clicks on a slow feed are two requests at once — and a sync is the one
    # console action that both fetches and writes, where running it twice over
    # means the second copy diffing against state the first has not committed
    # yet. Nothing here coordinates with the *poller*; that is a different
    # process and SQLite's write lock is what stands between them.
    in_flight: set[str] = set()
    in_flight_lock = threading.Lock()

    # --- guards ------------------------------------------------------------

    @app.hook("before_request")
    def _same_origin():
        """Reject cross-site writes.

        No cookies are used, so this is not session riding — but the console is
        a URL that a page in any other tab can post to, and nothing else stands
        between that page and a source being deleted.

        ``Sec-Fetch-Site`` is the check, not ``Origin`` against ``Host``. The
        browser computes it from the URL it is actually talking to, so it says
        ``same-origin`` for a form posting back to its own page whatever the
        hops in between do to the headers.

        Comparing ``Origin`` to ``Host`` looks equivalent and is not. Rewriting
        ``Host`` to the backend is ordinary reverse-proxy behaviour — nginx,
        Caddy, Traefik, Cloudflare Tunnel and tailscale serve all do it in some
        configuration — so that check refused every write from any browser not
        pointed straight at the port. It is not a property of one VPN.

        ``none`` means a user-initiated navigation with no initiator — a typed
        URL or a bookmark, which no attacker page can produce.

        The ``Origin`` fallback is coupled to ``layout.tpl``'s referrer policy.
        ``no-referrer`` makes a browser serialize ``Origin`` as ``null`` on every
        POST — Fetch, "append a request `Origin` header" — so the console's own
        forms arrived opaque and this refused every write it was ever reached
        for, on exactly the old browsers it exists to serve. It is
        ``same-origin`` for that reason: nothing leaks to an outbound link, and
        a write to the console still says where it came from.
        """
        if request.method not in ("POST", "PUT", "DELETE"):
            return

        site = request.headers.get("Sec-Fetch-Site")
        if site:
            if site in ("same-origin", "none"):
                return
            raise Refused(
                f"that form came from another site (Sec-Fetch-Site: {site}). Open "
                "the console at its own address and try again, rather than "
                "following a link in from somewhere else."
            )

        # No Sec-Fetch-Site: a browser too old to send it, something in front
        # stripping it, or not a browser at all. Fall back to Origin, and say
        # what mismatched — a refusal nobody can diagnose is one nobody gets
        # past, which is how this check burned an afternoon already.
        origin = request.headers.get("Origin")
        if not origin:
            return
        host = request.headers.get("Host") or ""
        sent = urlparse(origin).netloc
        if sent in {host, *trusted_origins}:
            return
        if not sent:
            # An opaque origin — a sandboxed frame, a page opened from a file,
            # or a referrer policy that suppresses it. It names no host, so
            # --trusted-origin has nothing to take; saying so beats printing the
            # flag with an empty value after it.
            raise Refused(
                f"that form came from an opaque origin ({origin}), which names no "
                f"host, so there is no --trusted-origin for it. If you are looking "
                f"at this console at {host!r} right now, that is a bug in calsync "
                "rather than something you did — its own pages are supposed to "
                "send their origin."
            )
        raise Refused(
            f"that form was submitted from {sent!r}, "
            f"but this console is being served as {host!r}. If both of those are "
            "yours, which is what happens when anything in front of calsync "
            "rewrites Host, start it with "
            f"--trusted-origin {sent}"
        )

    @app.error(500)
    def _oops(err):
        cause = getattr(err, "exception", None)
        if isinstance(cause, (Refused, OnboardingError, SecretError, FetchError,
                              InspectionError, config_mod.ConfigError,
                              repo.NotFound)):
            return render("problem.tpl", title="Stopped", message=str(cause))
        return render(
            "problem.tpl",
            title="Something broke",
            message="This is a bug in calsync, not something you did.",
            detail=err.traceback or traceback.format_exc(),
        )

    @app.error(404)
    def _missing(_err):
        return render(
            "problem.tpl",
            title="No such page",
            message="Nothing lives at that address.",
        )

    # --- dashboard ---------------------------------------------------------

    @app.get("/")
    def home():
        live = request.query.get("check") != "0"
        with connect() as conn:
            children = repo.list_children(conn)
            cards = [
                _card(conn, source)
                for source in repo.list_sources(conn, enabled_only=False)
            ]

        if live and cards:
            checked = _check_all(
                db_path, secrets, fetcher, clock, [c["source"] for c in cards]
            )
            for card in cards:
                _apply_check(card, checked.get(card["source"].id))

        return render(
            "home.tpl",
            cards=cards,
            children=children,
            live=live,
            flash=_flash(),
        )

    # --- onboarding --------------------------------------------------------

    @app.get("/onboard")
    def onboard_form():
        with connect() as conn:
            return render(
                "onboard.tpl",
                children=repo.list_children(conn),
                sports=repo.list_sports(conn),
                url=request.query.get("url", ""),
                flash=_flash(),
            )

    @app.post("/onboard")
    def onboard_inspect():
        """Fetch the URL and show what the feed says. Creates nothing."""
        url = _field("url").strip()
        if not url:
            raise Refused("paste a feed URL first")

        assembled = render_url(url, secrets=secrets, now=clock())
        found = inspect_feed(fetcher(assembled))
        plan = analyse_url(url)

        with connect() as conn:
            settings = Settings.load(conn)
            # Inspection has no database, so it cannot say which of these places
            # are already known. Answer that here — it is the difference between
            # "five venues" and "five venues, one of which is work".
            venues = tuple(
                replace(v, known=repo.resolve_venue_alias(conn, v.name) is not None)
                for v in found.venues
            )
            return render(
                "confirm.tpl",
                url=url,
                found=found,
                venues=venues,
                plan=plan,
                children=repo.list_children(conn),
                sports=repo.list_sports(conn),
                kinds=sources.available(),
                default_tz=settings.default_tz,
            )

    @app.post("/onboard/create")
    def onboard_create():
        with connect() as conn:
            child_id = _field("child")
            sport = _field("sport")
            # Clone-forward: last season's team in the same sport for the same
            # kid. Only the parts that survive a season change — never the name,
            # the feed or the aliases, which are exactly what churns.
            previous = clone_forward(conn, child_id, sport)
            draft = Draft(
                url=_field("url").strip(),
                child_id=child_id,
                sport=sport,
                team_name=_field("team_name").strip(),
                kind=_field("kind"),
                tz=(
                    _field("tz").strip()
                    or (previous.tz if previous else "")
                    or Settings.load(conn).default_tz
                ),
                token=_field("token").strip() or None,
                season_start=_field("season_start") or None,
                season_end=_field("season_end") or None,
                poll_interval_s=max(
                    _whole("check interval", _field("poll_interval_s"), default=1200), 60
                ),
                # getall has no unicode variant, but these keys are our own
                # ("path", "query:<name>") and never free text.
                secret_keys=tuple(request.forms.getall("vault")),
                staging_collection=_field("staging", "onboarding").strip(),
                emoji=previous.emoji if previous else None,
                league=previous.league if previous else None,
                age_group=previous.age_group if previous else None,
                alarm_game_min=previous.alarm_game_min if previous else 90,
                alarm_practice_min=previous.alarm_practice_min if previous else 30,
            )
            created = create_source(conn, draft, secrets=secrets)

        redirect(f"/sources/{created.source_id}?ok=" + _q("Created and staged."))

    # --- one source --------------------------------------------------------

    @app.get("/sources/<source_id>")
    def source_page(source_id):
        with connect() as conn:
            source = _require_source(conn, source_id)
            activity = repo.get_activity(conn, source.activity_id)
            child = repo.get_child(conn, activity.child_id)
            report = _preview(conn, source, secrets, fetcher, clock)
            conditions = gate.conditions(report, activity)
            state, label = gate.summarise(conditions)
            return render(
                "source.tpl",
                source=source,
                activity=activity,
                child=child,
                report=report,
                conditions=conditions,
                state=state,
                state_label=label,
                venues=repo.list_venues(conn),
                sports=repo.list_sports(conn),
                health=repo.source_row(conn, source_id),
                dormant=dormancy.for_source(conn, source_id, now=clock()),
                tracked=repo.tracked_events(conn, source_id),
                # What retiring would actually take off, as opposed to what this
                # source has on the calendar in total. The two are usually very
                # different by the time anybody retires anything.
                upcoming=retire.live_events(conn, source_id, now=clock()),
                polls=repo.recent_polls(conn, source_id),
                flash=_flash(),
            )

    @app.post("/activities/<activity_id>")
    def save_activity(activity_id):
        """Edit a team.

        Most of this is not cosmetic. `Activity.known_tokens` is built from the
        official name, short name, league and age group, and that is what turns
        "U10PL PSL Match vs Harbour FC" into an opponent instead of nothing — so
        these fields change how the feed parses on the next poll, not just how
        it reads.
        """
        back = _field("back") or "/"
        with connect() as conn:
            venue = _field("home_venue_id").strip()
            try:
                repo.update_activity(
                    conn, activity_id,
                    name=_field("name"),
                    emoji=_field("emoji").strip() or None,
                    official_name=_field("official_name").strip() or None,
                    short_name=_field("short_name").strip() or None,
                    league=_field("league").strip() or None,
                    age_group=_field("age_group").strip() or None,
                    home_venue_id=_whole("home venue", venue) if venue else None,
                    alarm_game_min=_whole("game alarm", _field("alarm_game_min"),
                                          default=90),
                    alarm_practice_min=_whole("practice alarm",
                                              _field("alarm_practice_min"), default=30),
                )
            except ValueError as exc:
                raise Refused(str(exc)) from exc
        redirect(f"{back}?ok=" + _q("Team saved. The next poll re-parses the feed."))

    @app.post("/sources/<source_id>/alias")
    def add_alias(source_id):
        alias = _field("alias").strip()
        if not alias:
            raise Refused("that answer was blank")
        with connect() as conn:
            source = _require_source(conn, source_id)
            repo.add_activity_alias(conn, source.activity_id, alias)
        redirect(f"/sources/{source_id}?ok=" + _q(f"Added {alias} as a name for this team."))

    @app.post("/sources/<source_id>/event-type")
    def teach_event_type(source_id):
        """Answer "what kind of event is this?" for one coach-typed label."""
        label = _field("label").strip()
        kind = _field("kind")
        if not label:
            raise Refused("that answer was blank")
        if kind not in ("game", "practice"):
            raise Refused("an event is either a game or a practice")
        with connect() as conn:
            _require_source(conn, source_id)
            repo.teach_event_type(conn, source_id, label, is_game=kind == "game")
        redirect(f"/sources/{source_id}?ok=" + _q(
            f"{label} now counts as a {kind} for this team."))

    @app.post("/sources/<source_id>/venue")
    def add_venue(source_id):
        """Answer "where is this?" — either a new place or another name for one.

        No coordinates are invented. A venue created here has a name and
        whatever address was typed, and stays unpinned until somebody confirms
        one; an unpinned location still reads correctly in a calendar.
        """
        raw = _field("raw").strip()
        existing = _field("existing").strip()
        name = _field("name").strip() or raw
        if not raw:
            raise Refused("nothing to name")

        with connect() as conn:
            _require_source(conn, source_id)
            if existing:
                config_mod.apply(conn, {"venues": [{"name": existing, "aliases": [raw]}]})
                message = f"{raw} is now another name for {existing}."
            else:
                config_mod.apply(
                    conn,
                    {
                        "venues": [
                            {
                                "name": name,
                                "address": _field("address").strip() or None,
                                "aliases": sorted({raw, name}),
                            }
                        ]
                    },
                )
                message = f"Added {name}."
        redirect(f"/sources/{source_id}?ok=" + _q(message))

    @app.post("/sources/<source_id>/promote")
    def promote(source_id):
        """Clear the staging collection, gated on a live check.

        Same gate as ``calsync promote``, and for the same reason it runs a
        fresh dry run rather than trusting anything stored: the feed may have
        grown a schedule since the page was rendered.
        """
        force = bool(_field("force"))
        with connect() as conn:
            source = _require_source(conn, source_id)
            if not source.staging_collection:
                redirect(f"/sources/{source_id}?ok=" + _q("Already live."))
            report = _preview(conn, source, secrets, fetcher, clock)
            if not report.promotable and not force:
                reason = (
                    "no games have appeared yet"
                    if report.fixtures_seen == 0
                    else "the parse still has gaps"
                )
                redirect(f"/sources/{source_id}?err=" + _q(f"Not promoted: {reason}."))
            repo.set_staging(conn, source_id, None)
        redirect(
            f"/sources/{source_id}?ok="
            + _q("Promoted. The next sync moves its events to the real calendars.")
        )

    @app.post("/sources/<source_id>/stage")
    def stage(source_id):
        collection = _field("collection", "onboarding").strip()
        with connect() as conn:
            _require_source(conn, source_id)
            repo.set_staging(conn, source_id, collection or None)
        redirect(f"/sources/{source_id}?ok=" + _q(f"Staged to {collection}."))

    @app.post("/sources/<source_id>/retire")
    def retire_source_route(source_id):
        """Clear what a source still has coming, then stop polling.

        Needs a real target rather than the previews behind the rest of this
        page, because removing an event is a write. "Sync now" is the other one.

        Events that have already happened stay put (`retire.py`), so retiring a
        season a month after it ended normally removes nothing and the message
        below says as much rather than claiming a success it did not have.
        """
        with connect() as conn:
            source = _require_source(conn, source_id)
            try:
                target = write_target or targeting.build_target(
                    conn, secrets=secrets
                )
                report = retire.retire_source(conn, source, target, now=clock())
            except (SecretError, TargetError) as exc:
                raise Refused(f"could not reach the calendar: {exc}") from exc

        if not report.ok:
            raise Refused(
                f"{len(report.errors)} event(s) could not be removed, so polling "
                "is still on and a later run will retry them: "
                + "; ".join(report.errors[:3])
            )
        if report.cancelled:
            done = f"{report.cancelled} upcoming event(s) removed from the calendar"
        else:
            done = "nothing was upcoming, so nothing came off the calendar"
        if report.kept:
            done += f"; {report.kept} past event(s) left in place"
        redirect(f"/sources/{source_id}?ok=" + _q(f"Polling stopped — {done}."))

    # --- syncing on demand -------------------------------------------------

    def _sync_now(source_ids: list[str]) -> tuple[list, list[str]]:
        """Run the real sync loop for the named sources, now.

        The same ``sync_source`` the poller calls, against the same target — not
        the dry run behind every other number on these pages. That is the whole
        point of the button: a dry run cannot put a venue you have just taught,
        or an answer you have just approved, on anybody's phone. Waiting for the
        next poll can, in twenty minutes.

        Two things it deliberately does not do. It does not touch the poller's
        schedule, so a forced sync is extra work rather than a skipped one — the
        feed is fetched twice in an hour, which is nothing, and the alternative
        is a button that quietly *delays* the next automatic poll. And it does
        not run the season-end, review or dispatch passes the poller runs after
        a sync: those decide whether to disable a source and whether to page
        somebody, and neither belongs on a key somebody pressed to see a change
        land.
        """
        taken: list[str] = []
        with in_flight_lock:
            for source_id in source_ids:
                if source_id not in in_flight:
                    in_flight.add(source_id)
                    taken.append(source_id)
        if not taken:
            raise Refused(
                "that is already syncing — give it a moment rather than "
                "starting a second run over the top of the first."
            )

        reports, problems = [], []
        try:
            with connect() as conn:
                try:
                    target = write_target or targeting.build_target(
                        conn, secrets=secrets
                    )
                except (SecretError, TargetError) as exc:
                    raise Refused(f"could not reach the calendar: {exc}") from exc

                for source_id in taken:
                    source = _require_source(conn, source_id)
                    try:
                        reports.append(
                            sync_source(
                                conn, source, target, now=clock(),
                                secrets=secrets, fetcher=fetcher,
                            )
                        )
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc):
                            raise
                        # The poller holds the write lock from its first
                        # recorded event to the end of the source, which is
                        # longer than the busy timeout when a feed is large.
                        # Nothing is lost: whatever reached the calendar before
                        # this is on it, and the next poll records the rest.
                        problems.append(
                            f"{source_id}: the poller was writing to the "
                            "database and would not let go in time — try again "
                            "in a moment."
                        )
        finally:
            with in_flight_lock:
                in_flight.difference_update(taken)
        return reports, problems

    @app.post("/sources/<source_id>/sync")
    def sync_one(source_id):
        with connect() as conn:
            source = _require_source(conn, source_id)
            if not source.enabled:
                # Refused rather than "resume, then sync": the events of a
                # retired season were cancelled on the way out, and a sync would
                # put every upcoming one back. Whoever paused it gets to say.
                raise Refused(
                    "polling is paused for this team, so there is nothing to "
                    "sync it against. Resume polling first — and if it was "
                    "retired rather than paused, be sure you want its events "
                    "back on the calendar."
                )

        reports, problems = _sync_now([source_id])
        if problems:
            redirect(f"/sources/{source_id}?err=" + _q("; ".join(problems)))
        redirect(f"/sources/{source_id}?ok=" + _q(reports[0].line()))

    @app.post("/sync")
    def sync_every():
        """Every enabled source, one after another.

        Serially, unlike the dashboard's checks: those are reads through their
        own connections, and this writes.
        """
        with connect() as conn:
            source_ids = [s.id for s in repo.list_sources(conn, enabled_only=True)]
        if not source_ids:
            redirect("/?err=" + _q("no team is being polled, so there is "
                                   "nothing to sync."))

        reports, problems = _sync_now(source_ids)
        lines = [r.line() for r in reports] + problems
        key = "err" if problems or any(r.status != "ok" for r in reports) else "ok"
        redirect(f"/?check=0&{key}=" + _q(" · ".join(lines)))

    @app.post("/sources/<source_id>/persists")
    def set_persists(source_id):
        """Mark a source as one that survives the off-season.

        Most teams are replaced each year, so a quiet feed means finished. A club
        team kept across seasons goes quiet every summer, and switching it off on
        a timer means finding out in September.
        """
        wanted = _field("persists") == "1"
        with connect() as conn:
            source = _require_source(conn, source_id)
            config = dict(source.config or {})
            config["persists_across_seasons"] = wanted
            conn.execute("UPDATE sources SET config = ? WHERE id = ?",
                         (json.dumps(config), source_id))
            conn.commit()
        redirect(f"/sources/{source_id}?ok=" + _q(
            "Kept across seasons; it will not be switched off automatically."
            if wanted else "Treated as a single season."))

    @app.post("/sources/<source_id>/enabled")
    def set_enabled(source_id):
        wanted = _field("enabled") == "1"
        with connect() as conn:
            _require_source(conn, source_id)
            repo.set_enabled(conn, source_id, wanted)
        redirect(
            f"/sources/{source_id}?ok="
            + _q("Polling resumed." if wanted else "Polling paused.")
        )

    # --- settings ----------------------------------------------------------

    #: Bounds on the guard thresholds. These are the protection against a
    #: truncated feed reading as a cancelled season, so the form will not let
    #: them be widened to the point of being off — a guard you can disable from
    #: a web form in two clicks is not a guard. Narrowing is always allowed.
    LIMITS = {
        "max_disappearance_pct": (0.01, 0.5),
        "max_disappearance_count": (1, 25),
        "sync_window_back_days": (0, 365),
        "sync_window_forward_days": (1, 1095),
    }

    def _bounded(key: str, raw: str) -> str:
        low, high = LIMITS[key]
        try:
            value = float(raw)
        except ValueError:
            raise Refused(f"{key} has to be a number, not {raw!r}") from None
        if value > 1 and key == "max_disappearance_pct":
            value = value / 100.0  # 20 and 0.20 both mean twenty percent
        if not low <= value <= high:
            raise Refused(
                f"{key} has to be between {low} and {high}. It is what stops a "
                "truncated feed from being read as a cancelled season, and "
                "widening it past that would leave nothing guarding a whole "
                "calendar's worth of deletions."
            )
        return str(value if key == "max_disappearance_pct" else int(value))

    def _settings_page(conn, check=None, pushed=None, calendar_check=None):
        settings = Settings.load(conn)
        return render(
            "settings.tpl",
            settings=settings,
            raw={r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")},
            sample=_sample_title(settings),
            matrix=matrix.load(conn),
            matrix_has_token=secrets.has(matrix.load(conn).secret_ref),
            pushover=notify.load(conn),
            pushover_ready=notify.load(conn).available(secrets),
            api_token_ref=Settings.load(conn).api_token_ref,
            api_has_token=secrets.has(Settings.load(conn).api_token_ref),
            pushed=pushed,
            radicale_has_password=secrets.has(settings.radicale_secret_ref),
            kinds=targeting.KINDS,
            check=check,
            calendar_check=calendar_check,
            flash=_flash(),
        )

    # --- the calendar ------------------------------------------------------

    @app.get("/calendar")
    def calendar_view():
        """What calsync has actually written, month by month.

        Read out of ``event_content`` — the receipt, the same source the digest
        and ``GET /v1/events`` read — and re-titled now through
        ``normalize/title.py``. Three things follow from that, and all three are
        the point rather than a compromise:

        - It shows what is **on the calendar**, not what a feed currently says.
          A poll held by a guard, or one that has not run yet, leaves the two
          different, and the calendar is the one somebody's phone has.
        - It re-composes each title on this request, so a naming-convention
          change shows up here without anything being re-fetched.
        - It cannot show a hand-created family appointment, because calsync
          never saw one. This is a view of calsync's own writes, and a page
          claiming to be the family's whole calendar would be lying about the
          half it does not manage.

        Bounded by what is retained: content is pruned to
        ``sync_window_back_days``, so a month further back than that is empty
        here and lives only on the calendar server.
        """
        # `mode` in here and in the template, `view` in the URL: `render()`'s
        # first parameter is the template name and is called `view`, so a
        # context key of that name collides with it.
        mode = "agenda" if request.query.get("view") == "agenda" else "month"
        child_id = request.query.get("child") or None

        with connect() as conn:
            settings = Settings.load(conn)
            children = repo.list_children(conn)
            zone = _zone(settings.default_tz)
            month = _month_of(request.query.get("month"), zone=zone, clock=clock)
            start, end = _month_bounds(month, zone)

            # Padded on both sides, then filtered exactly below. `stored_events`
            # compares ISO strings, and a feed may write an offset rather than
            # UTC — so a string bound can miss an event by its own offset. A day
            # either side covers every real one.
            items = repo.stored_events(
                conn,
                start=(start - timedelta(days=1)).isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                child_id=child_id,
            )

            colours, seen = {}, {}
            entries = []
            for item in items:
                if not start <= item.event.starts_at < end:
                    continue
                if item.activity_id not in seen:
                    activity = repo.get_activity(conn, item.activity_id)
                    seen[item.activity_id] = (
                        activity, [repo.get_child(conn, activity.child_id)]
                    )
                activity, kids = seen[item.activity_id]
                if activity.child_id not in colours:
                    colours[activity.child_id] = len(colours) % _COLOURS
                # In the venue's timezone, not the browser's and not the
                # household default: a game at 7pm local is on that day for the
                # people driving to it. Same rule the event bodies follow.
                local = item.event.local_start
                entries.append({
                    "uid": item.event.uid,
                    "local": local,
                    "day": local.date(),
                    "title": title_norm.render(item.event, activity, kids, settings),
                    "activity": activity,
                    "child": kids[0],
                    "colour": colours[activity.child_id],
                    "venue": item.event.venue,
                    "source_id": item.source_id,
                    "cancelled": item.cancelled,
                    "held": bool(settings.enrichment_collection)
                            and item.collection == settings.enrichment_collection,
                    "collection": item.collection,
                    "is_game": item.event.is_game,
                })

        def link(mode=mode, month=month, child=child_id):
            """One place that spells a link to this page, defaults included.

            Built here rather than in the template because every control on the
            page is the current view with one thing changed, and a template that
            reassembles three parameters by hand loses one of them the first
            time a fourth is added.
            """
            parts = [f"view={mode}", f"month={month.year:04d}-{month.month:02d}"]
            if child:
                parts.append(f"child={quote(child, safe='')}")
            return "/calendar?" + "&".join(parts)

        entries.sort(key=lambda e: (e["local"], e["title"]))
        by_day: dict[date, list] = {}
        for position, entry in enumerate(entries):
            # Numbered rather than keyed on the uid: a feed's uid is not ours to
            # assume anything about, and it ends up in a fragment identifier
            # that a chip in the grid links to in the agenda.
            entry["anchor"] = f"ev-{position}"
            by_day.setdefault(entry["day"], []).append(entry)

        return render(
            "calendar.tpl",
            mode=mode,
            month=month,
            weeks=calendar_mod.Calendar(firstweekday=0).monthdatescalendar(
                month.year, month.month
            ),
            by_day=by_day,
            entries=entries,
            link=link,
            children=children,
            child_id=child_id,
            today=clock().astimezone(zone).date(),
            previous=_shift_month(month, -1),
            following=_shift_month(month, +1),
            horizon=(clock().astimezone(zone)
                     - timedelta(days=settings.sync_window_back_days)).date(),
            tz=str(zone),
            flash=_flash(),
        )

    @app.get("/review")
    def review():
        """Everything waiting on a human, across every source.

        The questions come from a live dry run, exactly as the source page's
        gate does — a verdict from last week says nothing about a feed that has
        since been corrected. The *count* of what is actually held comes from
        `event_state`, because that is what is really sitting in the enrichment
        calendar rather than what a fresh parse thinks would be.

        Those two can disagree, and the disagreement is the useful part: events
        still held while the feed now parses cleanly are ones the next poll will
        release, and the page says so instead of showing an empty question list
        and leaving you wondering.
        """
        with connect() as conn:
            settings = Settings.load(conn)
            enrichment_collection = slugify(settings.enrichment_collection) \
                if settings.enrichment_collection else ""
            sources_ = repo.list_sources(conn, enabled_only=True)
            held = {
                s.id: repo.events_in_collection(conn, s.id, enrichment_collection)
                for s in sources_
            } if enrichment_collection else {}

        # Only the sources with something waiting, so the page is empty when
        # there is nothing to do rather than a list of everything that is fine.
        wanted = [s for s in sources_ if held.get(s.id)]
        reports = _check_all(db_path, secrets, fetcher, clock, wanted)

        queues = []
        with connect() as conn:
            for source in wanted:
                activity = repo.get_activity(conn, source.activity_id)
                report = reports.get(source.id)
                conditions = gate.conditions(report, activity) if report else ()
                queues.append({
                    "source": source,
                    "activity": activity,
                    "held": held.get(source.id, 0),
                    "report": report,
                    "asking": [c for c in conditions if c.state == gate.ASKING],
                })
            return render(
                "review.tpl",
                queues=queues,
                enrichment=enrichment_collection,
                # Answers waiting on a decision, across every source. Listed
                # separately from the questions because they are a different
                # act: answering is work, deciding is a glance.
                pending=repo.list_tasks(conn, state=repo.ANSWERED),
                venues=repo.list_venues(conn),
                flash=_flash(),
            )

    @app.post("/review/<task_id>/<decision:re:approve|reject>")
    def decide(task_id, decision):
        """The review gate, and the only place an answer becomes configuration.

        Reached from the console rather than the API on purpose: an agent can
        put an answer in front of you and has no path to this handler. Applying
        calls exactly the same `repo` helpers the manual answer forms call, so
        an approved answer and a hand-typed one write the same row.
        """
        with connect() as conn:
            task = repo.get_task(conn, task_id)
            if task is None:
                raise Refused("no such question")
            if task.state != repo.ANSWERED:
                raise Refused(
                    f"that question is {task.state}, so there is nothing waiting "
                    "on a decision"
                )
            if decision == "reject":
                repo.decide_task(conn, task_id, repo.REJECTED)
                conn.commit()
                redirect("/review?ok=" + _q("Rejected. The question stays open."))

            try:
                did = enrichment.apply(conn, task)
            except (enrichment.AnswerError, repo.NotFound) as exc:
                raise Refused(f"could not apply that answer: {exc}") from exc
            repo.decide_task(conn, task_id, repo.APPROVED)
            conn.commit()
        redirect("/review?ok=" + _q(
            f"Approved — {did}. The next poll re-renders and releases the events."))

    @app.get("/settings")
    def settings_page():
        with connect() as conn:
            return _settings_page(conn)

    @app.post("/settings/calendar")
    def save_calendar_settings():
        with connect() as conn:
            for key in ("target_kind",
                        "radicale_url", "radicale_user", "radicale_secret_ref",
                        "collection_template", "collection_game_label",
                        "collection_practice_label", "default_tz"):
                value = _field(key).strip()
                if value:
                    set_setting(conn, key, value)
            # Blank is a meaningful value here and nowhere else in this form:
            # it switches the hold off. The loop above deliberately ignores
            # empty fields so a half-filled form cannot wipe the CalDAV URL,
            # which would make this un-clearable if it went through the same
            # path — the setting would be documented as switchable and not be.
            set_setting(conn, "enrichment_collection",
                        _field("enrichment_collection").strip())
            password = _field("radicale_password")
            if password:
                secrets.put(_field("radicale_secret_ref").strip() or "radicale_password",
                            password)
        redirect("/settings?ok=" + _q("Calendar settings saved."))

    @app.post("/settings/titles")
    def save_title_settings():
        with connect() as conn:
            for key in ("title_template", "multi_kid_style", "all_kids_label",
                        "all_kids_threshold", "home_marker", "away_marker"):
                value = _field(key).strip()
                if value:
                    set_setting(conn, key, value)
        redirect("/settings?ok=" + _q("Title settings saved. Every event re-renders "
                                      "on the next sync — no re-fetch needed."))

    @app.post("/settings/seasons")
    def save_season_settings():
        with connect() as conn:
            for key in ("season_nudge_days", "season_shutoff_days"):
                value = _field(key).strip()
                if value:
                    set_setting(conn, key, str(_whole(key, value)))
            nudge = Settings.load(conn)
            if nudge.season_shutoff_days < nudge.season_nudge_days:
                raise Refused(
                    "the shut-off has to come after the nudge, or a season would "
                    "be switched off before anybody was told about it"
                )
        redirect("/settings?ok=" + _q("Season settings saved."))

    @app.post("/settings/notifications")
    def save_notification_settings():
        with connect() as conn:
            token_ref = _field("pushover_token_ref").strip() or "pushover_token"
            user_ref = _field("pushover_user_ref").strip() or "pushover_user"
            set_setting(conn, "pushover_token_ref", token_ref)
            set_setting(conn, "pushover_user_ref", user_ref)
            for ref, value in ((token_ref, _field("pushover_token")),
                               (user_ref, _field("pushover_user"))):
                if value:
                    secrets.put(ref, value)
        redirect("/settings?ok=" + _q("Notification settings saved."))

    @app.post("/settings/api")
    def save_api_settings():
        """The read API's bearer token.

        Stored in the secret store like every other credential, so the settings
        table stays safe to read, export and paste into a bug report. `calsync
        api` refuses to start until the value is there, which is why this form
        says whether it is rather than making you find out by starting it.
        """
        with connect() as conn:
            ref = _field("api_token_ref").strip() or "api_token"
            set_setting(conn, "api_token_ref", ref)
            token = _field("api_token")
            if token:
                secrets.put(ref, token)
        redirect("/settings?ok=" + _q("API settings saved."))

    @app.post("/settings/notifications/test")
    def test_notification():
        """Send a real push.

        Worth a button of its own: these credentials are used a handful of times
        a year, when a season ends, so a typo would otherwise sit undiscovered
        until the exact moment it needed to work.
        """
        with connect() as conn:
            config = notify.load(conn)
            try:
                notify.send(
                    config, secrets,
                    "If you can read this, calsync can reach you when a season ends.",
                    title="calsync test", opener=push_opener,
                )
                result = "Sent. Check your phone."
            except notify.NotifyError as exc:
                result = str(exc)
            return _settings_page(conn, pushed=result)

    @app.post("/settings/safety")
    def save_safety_settings():
        with connect() as conn:
            for key in LIMITS:
                value = _field(key).strip()
                if value:
                    set_setting(conn, key, _bounded(key, value))
        redirect("/settings?ok=" + _q("Safety settings saved."))

    @app.post("/settings/matrix")
    def save_matrix_settings():
        ref = _field("matrix_secret_ref").strip() or "matrix_access_token"
        with connect() as conn:
            set_setting(conn, "matrix_homeserver", _field("matrix_homeserver").strip())
            set_setting(conn, "matrix_user_id", _field("matrix_user_id").strip())
            set_setting(conn, "matrix_room_id", _field("matrix_room_id").strip())
            set_setting(conn, "matrix_secret_ref", ref)
            # Validated on the way in: `digest.due` treats anything it cannot
            # parse as "never", so a typo would otherwise look configured and
            # quietly send nothing for the rest of the season.
            send_at = _field("digest_send_at").strip()
            if send_at and not _reads_as_a_time(send_at):
                raise Refused(
                    f"{send_at!r} is not a time of day — use HH:MM, like 07:30, "
                    "or leave it empty for no digest"
                )
            set_setting(conn, "digest_send_at", send_at)
            hours = _field("digest_window_hours").strip()
            if hours:
                set_setting(conn, "digest_window_hours", str(_whole("hours", hours)))
            # The token is a bearer credential; it goes to the secret store and
            # never to the database, same as the Radicale password.
            token = _field("matrix_access_token")
            if token:
                secrets.put(ref, token)
        redirect("/settings?ok=" + _q("Matrix settings saved. Check them below."))

    @app.post("/settings/calendar/verify")
    def verify_calendar_settings():
        """Ask the calendar server whether this configuration is real.

        The check nothing did until a deployment spent days writing nothing,
        because `radicale_url` pointed at localhost and the poller runs in a
        container where localhost is itself. The failure was in the logs, once
        per source per poll, behind a wall of per-event errors — and then the
        backoff made it three-hourly. A button is what turns that into a
        question somebody can ask.
        """
        with connect() as conn:
            return _settings_page(
                conn, calendar_check=targeting.verify(
                    conn, secrets, transport=calendar_transport
                )
            )

    @app.post("/settings/matrix/verify")
    def verify_matrix_settings():
        with connect() as conn:
            return _settings_page(
                conn, check=matrix.verify(matrix.load(conn), secrets, opener=matrix_opener)
            )

    # --- venues ------------------------------------------------------------
    #
    # Worth a screen of its own because venues are the one thing that outlasts a
    # team. Names churn every season; the parks and schools do not, so an alias
    # added once is still paying off three seasons later — and near-duplicates
    # accumulating across those seasons is the normal way this table goes wrong.

    @app.get("/venues")
    def venues_index():
        with connect() as conn:
            return render(
                "venues.tpl", venues=repo.venues_detailed(conn), flash=_flash()
            )

    @app.get("/venues/<venue_id:int>")
    def venue_page(venue_id):
        with connect() as conn:
            venue = repo.get_venue_detail(conn, venue_id)
            if venue is None:
                raise Refused("there is no venue with that id")
            return render(
                "venue.tpl",
                venue=venue,
                others=[v for v in repo.venues_detailed(conn) if v.id != venue_id],
                flash=_flash(),
            )

    def _checked_name(raw: str) -> str:
        """Refuse a field designator in a venue name.

        "Kingsmere #2" is field #2 at Kingsmere, and folding the designator into
        the name mints a separate venue — and a separate pin — for every field
        at one park. The parser already splits these apart; this stops a typed
        name from putting one back.
        """
        name = raw.strip()
        if not name:
            raise Refused("a venue needs a name")
        base, designator = venue_norm.split_field(name)
        if designator and base:
            raise Refused(
                f"{name!r} is {designator} at {base!r}. A venue is the place, not the "
                f"field within it — otherwise every field at {base!r} gets its own "
                f"row and its own pin. Save it as {base!r}; {name!r} can be one of "
                "its other names."
            )
        return name


    @app.post("/venues")
    def create_venue():
        name = _checked_name(_field("name"))
        with connect() as conn:
            venue_id = repo.upsert_venue(
                conn,
                name=name,
                short_name=_field("short_name").strip() or None,
                address=_field("address").strip() or None,
            )
        redirect(f"/venues/{venue_id}?ok=" + _q(f"Added {name}."))

    @app.post("/venues/<venue_id:int>")
    def save_venue(venue_id):
        name = _checked_name(_field("name"))
        with connect() as conn:
            existing = repo.get_venue_detail(conn, venue_id)
            if existing is None:
                raise Refused("there is no venue with that id")
            repo.upsert_venue(
                conn,
                venue_id=venue_id,
                name=name,
                short_name=_field("short_name").strip() or None,
                address=_field("address").strip() or None,
                # Carried, not edited. Nothing emits coordinates any more, so
                # the console has no business curating them — but a value that
                # arrived through config import is not the console's to discard.
                lat=existing.lat,
                lon=existing.lon,
                pin_confirmed=existing.pin_confirmed,
                geocoder=existing.geocoder,
            )
        redirect(f"/venues/{venue_id}?ok=" + _q(f"Saved {name}."))


    @app.post("/venues/<venue_id:int>/alias")
    def venue_alias(venue_id):
        alias = _field("alias").strip()
        remove = _field("remove").strip()
        with connect() as conn:
            if remove:
                venue = repo.get_venue_detail(conn, venue_id)
                if venue and remove == venue.name:
                    raise Refused(
                        "that is the venue's own name. Rename it and this alias "
                        "goes with it."
                    )
                repo.remove_venue_alias(conn, venue_id, remove)
                message = f"Removed {remove}."
            else:
                if not alias:
                    raise Refused("that was blank")
                repo.add_venue_alias(conn, venue_id, alias)
                message = f"{alias} now resolves here."
        redirect(f"/venues/{venue_id}?ok=" + _q(message))

    @app.post("/venues/<venue_id:int>/merge")
    def merge_venue(venue_id):
        target = _field("into").strip()
        if not target:
            raise Refused("pick a venue to merge into")
        with connect() as conn:
            losing = repo.get_venue_detail(conn, venue_id)
            winning = repo.get_venue_detail(conn, _whole("venue", target))
            if losing is None or winning is None:
                raise Refused("one of those venues no longer exists")
            moved = repo.merge_venues(conn, losing_id=venue_id, winning_id=winning.id)
        redirect(
            f"/venues/{winning.id}?ok="
            + _q(f"Merged {losing.name} in, keeping {moved + 1} names for this place.")
        )

    @app.post("/venues/<venue_id:int>/delete")
    def remove_venue(venue_id):
        with connect() as conn:
            venue = repo.get_venue_detail(conn, venue_id)
            if venue is None:
                raise Refused("there is no venue with that id")
            repo.delete_venue(conn, venue_id)
            note = (
                f" {', '.join(venue.home_to)} lost its home ground."
                if venue.home_to
                else ""
            )
        redirect("/venues?ok=" + _q(f"Removed {venue.name}.{note}"))

    # --- the household -----------------------------------------------------
    #
    # Not admin CRUD over tables — only the handful of things that genuinely
    # change. A kid is added once and then occasionally corrected; the sport
    # catalog needs a row when a household takes up something the seed list
    # never anticipated, and that sport's emoji is in the title of every event.

    @app.get("/household")
    def household():
        with connect() as conn:
            return render(
                "household.tpl",
                children=[
                    {
                        "row": row,
                        "usage": repo.child_usage(conn, row["id"]),
                        # Decoded here rather than in the template: the column is
                        # a JSON array, and a view has no business parsing one.
                        "nicknames": ", ".join(json.loads(row["nicknames"] or "[]")),
                    }
                    for row in repo.child_rows(conn)
                ],
                sports=[
                    {"row": row, "usage": repo.sport_usage(conn, row["id"])}
                    for row in repo.list_sports(conn)
                ],
                flash=_flash(),
            )

    @app.post("/children")
    def save_child():
        """Create or correct. An existing id means a correction.

        The id is derived from the name once and then left alone: activities
        reference it, and fixing a spelling should fix the calendar titles
        rather than rebuild every row that points at them.
        """
        name = _field("name").strip()
        if not name:
            raise Refused("a kid needs a name")

        child_id = _field("id").strip() or slugify(name)
        initial = _field("initial").strip() or name[:1].upper()
        nicknames = [
            n.strip() for n in _field("nicknames").split(",") if n.strip()
        ]

        with connect() as conn:
            order = _field("birth_order") or conn.execute(
                "SELECT COALESCE(MAX(birth_order), 0) + 1 AS n FROM children"
            ).fetchone()["n"]
            try:
                config_mod.apply(
                    conn,
                    {
                        "children": [
                            {
                                "id": child_id,
                                "name": name,
                                "initial": initial,
                                "birth_order": _whole("list order", str(order)),
                                "color": _field("color").strip() or None,
                                "nicknames": nicknames,
                            }
                        ]
                    },
                )
            except sqlite3.IntegrityError as exc:
                # The only unique constraint here is on `initial`, and it exists
                # so a shared title can never render ambiguously.
                raise Refused(
                    f"{initial!r} is already another kid's initial, and initials have "
                    'to be unique — a title like "P+J" has to mean one pair of kids.'
                ) from exc

        redirect(
            _field("next")
            or f"/household?ok={_q(f'Saved {name}.')}#{child_id}"
        )

    @app.post("/children/<child_id>/delete")
    def remove_child(child_id):
        with connect() as conn:
            usage = repo.child_usage(conn, child_id)
            if usage.in_use:
                raise Refused(
                    f"{child_id} still has {len(usage.activities)} team(s): "
                    f"{', '.join(usage.activities)}. Deleting a kid cascades through "
                    f"their teams to the record of the {usage.tracked_events} events "
                    "already written, which would leave those events sitting in the "
                    "calendar with nothing tracking them. Remove the teams first."
                )
            repo.delete_child(conn, child_id)
        redirect("/household?ok=" + _q(f"Removed {child_id}."))

    @app.post("/sports")
    def save_sport():
        name = _field("name").strip()
        emoji = _field("emoji").strip()
        if not name or not emoji:
            raise Refused("a sport needs a name and an emoji")

        sport_id = _field("id").strip() or slugify(name).replace("-", "_")
        with connect() as conn:
            config_mod.apply(
                conn, {"sports": [{"id": sport_id, "name": name, "emoji": emoji}]}
            )
        redirect(f"/household?ok={_q(f'Saved {name}.')}#sport-{sport_id}")

    @app.post("/sports/<sport_id>/delete")
    def remove_sport(sport_id):
        with connect() as conn:
            sport = repo.get_sport(conn, sport_id)
            if sport is None:
                raise Refused(f"there is no sport called {sport_id!r}")
            if sport["builtin"]:
                raise Refused(
                    f"{sport['name']} is built in, and the seed list is reapplied on "
                    "every upgrade — deleting it would only bring it back. Change its "
                    "emoji instead; that edit does survive."
                )
            usage = repo.sport_usage(conn, sport_id)
            if usage.in_use:
                raise Refused(
                    f"{sport['name']} is still used by {', '.join(usage.activities)}."
                )
            repo.delete_sport(conn, sport_id)
        redirect("/household?ok=" + _q(f"Removed {sport_id}."))

    # --- static ------------------------------------------------------------

    @app.get("/static/<path:path>")
    def assets(path):
        return static_file(path, root=str(STATIC))

    return app


# --- helpers ----------------------------------------------------------------


def _whole(name: str, value: str, *, default: int | None = None) -> int:
    """A form field as an integer, or a message the operator can act on.

    Every one of these arrives from a select or a number input, so a
    non-numeric value means a hand-built POST or a browser doing something
    odd — either way it is bad input, not a bug, and rendering it as
    "This is a bug in calsync" over a traceback tells the operator the wrong
    thing about their own console.
    """
    value = (value or "").strip()
    if not value and default is not None:
        return default
    try:
        return int(value)
    except ValueError:
        raise Refused(f"{name} has to be a whole number, not {value!r}") from None


def _reads_as_a_time(value: str) -> bool:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _field(name: str, default: str = "") -> str:
    """One form value, decoded as UTF-8.

    Bottle leaves form values latin-1 decoded — the HTTP default when a browser
    omits the charset, which browsers always do — and exposes ``getunicode`` to
    recode them. Reading them with plain ``.get`` mangles every emoji and every
    accented name, so nothing in this module calls ``.get`` on a form.
    """
    return request.forms.getunicode(name) or default


def _sample_title(settings) -> str:
    """Render the title template against a made-up event.

    The template fields are the least guessable setting in the table, and the
    title is a render rather than data — so changing it re-renders every event
    on the next sync without a re-fetch. Showing the result is much cheaper than
    changing it, syncing, and looking at a phone.
    """
    from ..models import Activity, Child, Event, Venue
    from ..normalize import title

    when = datetime(2026, 4, 11, 14, 0, tzinfo=timezone.utc)
    child = Child(id="sample", name="Parker", initial="P", birth_order=1)
    activity = Activity(
        id="sample", child_id="sample", name="Meteors", sport="soccer",
        emoji="\u26bd\ufe0f", tz="UTC",
    )
    event = Event(
        uid="sample", activity_id="sample", starts_at=when, ends_at=when,
        is_game=True, tz="UTC", opponent="Chargers", home=True,
        # Every field the template offers is populated, so an unused one reads
        # as unused rather than as broken.
        venue=Venue(raw="Kingsmere Meadow Park", name="Kingsmere Meadow Park"),
    )
    try:
        return title.render(event, activity, [child], settings)
    except (KeyError, IndexError, ValueError) as exc:
        # An unknown {field} in the template. Say so rather than 500 — the
        # operator is mid-edit and needs to see which field is wrong.
        return f"template error: {exc}"


def render(view: str, **context) -> str:
    context.setdefault("flash", None)
    return template(view, template_lookup=[str(VIEWS)], **context)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _q(value: str) -> str:
    """Make a message safe to hand back through a redirect query string."""
    return quote(" ".join(value.split()), safe="")


#: How many child colours the calendar cycles through before repeating. Six is
#: past any household this is built for; the wrap is there so a seventh kid gets
#: a colour somebody else has rather than no colour at all.
_COLOURS = 6


def _zone(name: str) -> ZoneInfo:
    """The household's timezone, or UTC if the setting names one that is gone.

    A tzdata that has dropped a zone must not take the calendar page down —
    `default_tz` is a free-text setting, and the page's job is to show a
    schedule, not to adjudicate the zone database.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _month_of(value: str | None, *, zone: ZoneInfo, clock) -> date:
    """The first of the month a ``YYYY-MM`` parameter names. Today's by default.

    A malformed one falls back rather than refusing: this arrives from a link,
    and an error page in place of a calendar helps nobody.
    """
    if value:
        try:
            year, month = (int(part) for part in value.split("-", 1))
            return date(year, month, 1)
        except (ValueError, TypeError):
            pass
    return clock().astimezone(zone).date().replace(day=1)


def _month_bounds(month: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """The month as an absolute half-open span, local midnight to local midnight."""
    start = datetime(month.year, month.month, 1, tzinfo=zone)
    following = _shift_month(month, +1)
    return start, datetime(following.year, following.month, 1, tzinfo=zone)


def _shift_month(month: date, by: int) -> date:
    index = month.year * 12 + (month.month - 1) + by
    return date(index // 12, index % 12 + 1, 1)


def _flash():
    ok, err = request.query.get("ok"), request.query.get("err")
    if ok:
        return {"kind": "good", "text": ok}
    if err:
        return {"kind": "bad", "text": err}
    return None


def _require_source(conn, source_id: str):
    source = repo.get_source(conn, source_id)
    if source is None:
        raise Refused(f"there is no source called {source_id!r}")
    return source


def _preview(conn, source, secrets, fetcher, clock):
    """A live dry run: the truth about this feed right now.

    Deliberately not cached and deliberately not stored. ``calsync promote``
    re-checks for the same reason — a verdict from last week says nothing about
    a feed that has since grown a fixture list.
    """
    return sync_source(
        conn, source, _NoTarget(), now=clock(), secrets=secrets,
        fetcher=fetcher, dry_run=True,
    )


def _card(conn, source) -> dict:
    activity = repo.get_activity(conn, source.activity_id)
    return {
        "source": source,
        "activity": activity,
        "child": repo.get_child(conn, activity.child_id),
        "tracked": repo.tracked_events(conn, source.id),
        "health": repo.source_row(conn, source.id),
        "conditions": (),
        "state": "unchecked",
        "state_label": "not checked",
        "report": None,
        "feed_events": 0,
        "dormant": dormancy.for_source(conn, source.id),
    }


def _apply_check(card: dict, report) -> None:
    if report is None:
        return
    conditions = gate.conditions(report, card["activity"])
    state, label = gate.summarise(conditions)
    card.update(
        conditions=conditions,
        state=state,
        state_label=label,
        report=report,
        # What the feed holds right now, which is not the same as what is on the
        # calendar: events outside the sync window are read and then dropped.
        feed_events=report.created + report.updated + report.unchanged,
    )


def _check_all(db_path: str, secrets, fetcher, clock, sources) -> dict:
    """Dry-run every source at once, so the dashboard is current rather than stale.

    One connection per worker: a sqlite3 connection belongs to the thread that
    opened it. Failures are swallowed to None — one dead feed must not take the
    page down, and the card says "not checked" rather than inventing a verdict.
    """
    if not sources:
        return {}

    def check(source):
        try:
            with closing(db.connect(db_path)) as conn:
                fresh = repo.get_source(conn, source.id)
                if fresh is None or not fresh.enabled:
                    return source.id, None
                return source.id, _preview(conn, fresh, secrets, fetcher, clock)
        except Exception:  # noqa: BLE001 - a broken feed is not a broken page
            return source.id, None

    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        return dict(pool.map(check, sources))


# --- server -----------------------------------------------------------------


class _Threaded(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        """Do not log request lines.

        A URL here can carry a team name and a child's name, and this process
        writes to the container log. The poller already reports what matters.
        """


def serve(app, *, host: str = "127.0.0.1", port: int = 8730) -> None:
    """Run the console.

    Threaded because inspecting a feed blocks on a network fetch for as long as
    the fetch timeout, and a single-threaded server would freeze the whole
    console while one feed is slow.

    The default host is loopback and should stay that way: this serves
    children's names, schedules and the physical locations they will be at.
    Reach it from another machine by putting a VPN or an authenticating proxy in
    front of it, not by binding this to 0.0.0.0 — whichever one you use, the
    console itself has no login and is not built to face a network directly.
    """
    bottle.TEMPLATE_PATH = [str(VIEWS)]
    with make_server(
        host, port, app, server_class=_Threaded, handler_class=_QuietHandler
    ) as httpd:
        print(f"calsync console on http://{host}:{port}", flush=True)
        httpd.serve_forever()
