"""Routes. Thin on purpose — the work is in inspection, onboarding and gate."""

from __future__ import annotations

import json
import socketserver
import sqlite3
import urllib.request
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import bottle
from bottle import Bottle, redirect, request, static_file, template

from .. import config as config_mod
from .. import db, matrix, repo, retire, sources, targeting
from ..fetch import FetchError, http_fetch, render_url
from ..inspection import InspectionError, inspect_feed
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
    retire_target=None,
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
    """
    app = Bottle()
    secrets = secrets or SecretStore()
    clock = clock or _now
    trusted_origins = set(trusted_origins or ())
    # A second network seam, separate from `fetcher` because it speaks a
    # different protocol: feeds are GETs of iCalendar text, this is an
    # authenticated JSON API.
    matrix_opener = matrix_opener or urllib.request.urlopen
    db_path = str(db_path)

    # Once, at startup — not per request. Migration takes the write lock.
    with closing(db.open_db(db_path)):
        pass

    def connect():
        return closing(db.connect(db_path))

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
        if urlparse(origin).netloc in {host, *trusted_origins}:
            return
        raise Refused(
            f"that form was submitted from {urlparse(origin).netloc or origin!r}, "
            f"but this console is being served as {host!r}. If both of those are "
            "yours, which is what happens when anything in front of calsync "
            "rewrites Host, start it with "
            f"--trusted-origin {urlparse(origin).netloc}"
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
                health=repo.source_row(conn, source_id),
                tracked=repo.tracked_events(conn, source_id),
                polls=repo.recent_polls(conn, source_id),
                flash=_flash(),
            )

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
        """The end of a season: clear the calendar, then stop polling.

        Needs a real target, unlike everything else on this page — it is the one
        console action that writes to the family's calendars, because removing
        an event is a write. A preview cannot do it.
        """
        with connect() as conn:
            source = _require_source(conn, source_id)
            try:
                target = retire_target or targeting.build_target(
                    conn, secrets=secrets
                )
                report = retire.retire_source(conn, source, target)
            except (SecretError, TargetError) as exc:
                raise Refused(f"could not reach the calendar: {exc}") from exc

        if not report.ok:
            raise Refused(
                f"{len(report.errors)} event(s) could not be removed, so polling "
                "is still on and a later run will retry them: "
                + "; ".join(report.errors[:3])
            )
        redirect(f"/sources/{source_id}?ok=" + _q(
            f"Retired. {report.cancelled} events removed from the calendar and "
            "polling stopped."))

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

    def _settings_page(conn, check=None):
        settings = Settings.load(conn)
        return render(
            "settings.tpl",
            settings=settings,
            raw={r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")},
            sample=_sample_title(settings),
            matrix=matrix.load(conn),
            matrix_has_token=secrets.has(matrix.load(conn).secret_ref),
            radicale_has_password=secrets.has(settings.radicale_secret_ref),
            kinds=targeting.KINDS,
            check=check,
            flash=_flash(),
        )

    @app.get("/settings")
    def settings_page():
        with connect() as conn:
            return _settings_page(conn)

    @app.post("/settings/calendar")
    def save_calendar_settings():
        with connect() as conn:
            for key in ("target_kind", "radicale_url", "radicale_user",
                        "radicale_secret_ref",
                        "collection_template", "collection_game_label",
                        "collection_practice_label", "default_tz"):
                value = _field(key).strip()
                if value:
                    set_setting(conn, key, value)
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
            # The token is a bearer credential; it goes to the secret store and
            # never to the database, same as the Radicale password.
            token = _field("matrix_access_token")
            if token:
                secrets.put(ref, token)
        redirect("/settings?ok=" + _q("Matrix settings saved. Check them below."))

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

    #: How a ``venues.geocoder`` value reads to a person. The column records
    #: where a pin came from, which is the whole basis for trusting it.
    SOURCE_OF_PIN = {
        "ui": "you, typed in here",
        "config": "the imported config file",
        "merge": "a venue this one was merged with",
    }

    @app.get("/venues/<venue_id:int>")
    def venue_page(venue_id):
        with connect() as conn:
            venue = repo.get_venue_detail(conn, venue_id)
            if venue is None:
                raise Refused("there is no venue with that id")
            return render(
                "venue.tpl",
                venue=venue,
                pin_source=SOURCE_OF_PIN.get(venue.geocoder, venue.geocoder),
                others=[v for v in repo.venues_detailed(conn) if v.id != venue_id],
                flash=_flash(),
            )

    def _checked_name(raw: str) -> str:
        """Refuse a field designator in a venue name.

        "Riverview #2" is field #2 at Riverview, and folding the designator into
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

    def _coords():
        """Latitude and longitude, or nothing. Never a partial pin."""
        lat, lon = _field("lat").strip(), _field("lon").strip()
        if not lat and not lon:
            return None, None
        try:
            pair = (float(lat), float(lon))
        except ValueError:
            raise Refused("coordinates have to be two decimal numbers") from None
        if not (-90 <= pair[0] <= 90 and -180 <= pair[1] <= 180):
            raise Refused(f"{pair[0]}, {pair[1]} is not a point on Earth")
        return pair

    @app.post("/venues")
    def create_venue():
        name = _checked_name(_field("name"))
        lat, lon = _coords()
        with connect() as conn:
            venue_id = repo.upsert_venue(
                conn,
                name=name,
                short_name=_field("short_name").strip() or None,
                address=_field("address").strip() or None,
                lat=lat,
                lon=lon,
                # A person typing coordinates in is the confirmation. The rule
                # this respects is that nothing a *model* proposes counts as
                # confirmed until a human has looked at it.
                pin_confirmed=lat is not None,
                geocoder="ui" if lat is not None else None,
            )
        redirect(f"/venues/{venue_id}?ok=" + _q(f"Added {name}."))

    @app.post("/venues/<venue_id:int>")
    def save_venue(venue_id):
        name = _checked_name(_field("name"))
        lat, lon = _coords()
        with connect() as conn:
            existing = repo.get_venue_detail(conn, venue_id)
            if existing is None:
                raise Refused("there is no venue with that id")
            # Keep a pin somebody already confirmed confirmed, and do not let
            # editing an address quietly promote an unconfirmed one.
            confirmed = existing.pin_confirmed
            geocoder = existing.geocoder
            if (lat, lon) != (existing.lat, existing.lon):
                confirmed = lat is not None
                geocoder = "ui" if lat is not None else None
            repo.upsert_venue(
                conn,
                venue_id=venue_id,
                name=name,
                short_name=_field("short_name").strip() or None,
                address=_field("address").strip() or None,
                lat=lat,
                lon=lon,
                pin_confirmed=confirmed,
                geocoder=geocoder,
            )
        redirect(f"/venues/{venue_id}?ok=" + _q(f"Saved {name}."))

    @app.post("/venues/<venue_id:int>/confirm")
    def confirm_pin(venue_id):
        """Vouch for coordinates something else proposed."""
        with connect() as conn:
            venue = repo.get_venue_detail(conn, venue_id)
            if venue is None or not venue.pinned:
                raise Refused("there is no pin here to confirm")
            repo.upsert_venue(
                conn,
                venue_id=venue_id,
                name=venue.name,
                short_name=venue.short_name,
                address=venue.address,
                lat=venue.lat,
                lon=venue.lon,
                pin_confirmed=True,
                geocoder=venue.geocoder,
            )
        redirect(f"/venues/{venue_id}?ok=" + _q("Pin confirmed."))

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
    child = Child(id="sample", name="Patrick", initial="P", birth_order=1)
    activity = Activity(
        id="sample", child_id="sample", name="Rockets", sport="soccer",
        emoji="\u26bd\ufe0f", tz="UTC",
    )
    event = Event(
        uid="sample", activity_id="sample", starts_at=when, ends_at=when,
        is_game=True, tz="UTC", opponent="Strikers", home=True,
        # Every field the template offers is populated, so an unused one reads
        # as unused rather than as broken.
        venue=Venue(raw="Riverview Farm Park", name="Riverview Farm Park"),
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
