"""Serve the test fixtures as live feeds, for looking at the console.

Not part of the product. This exists because the four recorded feeds are from
spring 2026 and the sync window is seven days back — replay them as-is and every
event falls outside the window, so the console shows an empty parse and every
gate condition passes vacuously. That is the least interesting thing it can do.

So the dates are shifted forward **in whole weeks**, which keeps every practice
on the weekday the coach actually chose, and lands the season across today: some
of it behind, most of it ahead.

    python3 demo/feeds.py --port 8000

Then paste http://localhost:8000/hawks.ics into the console.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: path -> (fixture, what it demonstrates)
FEEDS: dict[str, tuple[str, str]] = {
    "/hawks.ics": (
        "teamreach_hawks_sample.ics",
        "US vs THEM. The team name has to be read out of fixture frequency; "
        "get it wrong and 12 fixtures go unmatched.",
    ),
    "/comets.ics": (
        "teamreach_comets_sample.ics",
        "TYPE vs OPPONENT. Parses cleanly with no team name at all — only the "
        "venues need answering.",
    ),
    "/hurricanes.ics": (
        "teamreach_sample.ics",
        "TYPE - VENUE. No LOCATION field anywhere; the venue is the summary tail.",
    ),
    "/rush.ics": (
        "player360_sample.ics",
        "A different platform: CATEGORIES, inline street addresses, a generic "
        "calendar name.",
    ),
    "/practices-only.ics": (
        "teamreach_comets_sample.ics",
        "The same feed in March, before the coach posted the schedule. The "
        "resting state the console is designed around.",
    ),
}

#: How far before today the season should start, so the calendar has both a
#: recent past and a future.
LEAD = timedelta(days=21)

_STAMP = re.compile(rb"^(DTSTART|DTEND|DTSTAMP|LAST-MODIFIED):(\d{8}T\d{6}Z)", re.MULTILINE)
_FMT = "%Y%m%dT%H%M%SZ"

_VEVENT = re.compile(rb"BEGIN:VEVENT.*?END:VEVENT\r?\n?", re.DOTALL)
_SUMMARY = re.compile(rb"^SUMMARY:(.*)$", re.MULTILINE)
_FIXTURE = re.compile(rb"\bvs\.?\b|\s@\s|\bgame\b", re.IGNORECASE)


def _shift(raw: bytes, *, now: datetime) -> bytes:
    stamps = [
        datetime.strptime(m.group(2).decode(), _FMT).replace(tzinfo=timezone.utc)
        for m in _STAMP.finditer(raw)
        if m.group(1) == b"DTSTART"
    ]
    if not stamps:
        return raw

    # Whole weeks only. A Tuesday practice has to stay on a Tuesday.
    wanted = (now - LEAD) - min(stamps)
    offset = timedelta(weeks=round(wanted.days / 7))

    def move(match: re.Match) -> bytes:
        moment = datetime.strptime(match.group(2).decode(), _FMT).replace(
            tzinfo=timezone.utc
        )
        return match.group(1) + b":" + (moment + offset).strftime(_FMT).encode()

    return _STAMP.sub(move, raw)


def _practices_only(raw: bytes) -> bytes:
    """Drop every fixture, leaving what a coach posts first.

    Substituting whole VEVENT blocks rather than splitting on them, so the
    calendar header and its END:VCALENDAR survive — the last event in these
    feeds is a game, and taking it out by splitting takes the terminator with
    it, which yields an unparseable feed rather than a practices-only one.
    """

    def drop(match: re.Match) -> bytes:
        summary = _SUMMARY.search(match.group(0))
        line = summary.group(1) if summary else b""
        return b"" if _FIXTURE.search(line) else match.group(0)

    return _VEVENT.sub(drop, raw)


def body(path: str, *, now: datetime) -> bytes:
    fixture, _ = FEEDS[path]
    raw = (FIXTURES / fixture).read_bytes()
    if path == "/practices-only.ics":
        raw = _practices_only(raw)
    return _shift(raw, now=now)


INDEX = """<!doctype html><meta charset=utf-8>
<title>calsync demo feeds</title>
<style>
 body{{font:16px/1.6 ui-sans-serif,system-ui;max-width:44rem;margin:3rem auto;padding:0 1.5rem}}
 h1{{font-size:1.4rem}} li{{margin-bottom:1.1rem}}
 code{{font:0.85rem ui-monospace,monospace;background:#eee;padding:.1rem .3rem}}
</style>
<h1>Demo feeds</h1>
<p>Recorded feeds, shifted forward in whole weeks so they straddle today.
   Paste one into the console.</p>
<ul>{items}</ul>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in FEEDS:
            return self._send(body(path, now=datetime.now(timezone.utc)), "text/calendar")
        if path == "/":
            items = "".join(
                f"<li><code>http://{self.headers.get('Host', 'localhost')}{p}</code>"
                f"<br>{note}</li>"
                for p, (_, note) in FEEDS.items()
            )
            return self._send(INDEX.format(items=items).encode(), "text/html")
        self.send_error(404)

    def _send(self, payload: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print(f"  feed {self.path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"demo feeds on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
