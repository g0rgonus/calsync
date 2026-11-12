# Changelog

All notable changes to calsync are recorded here, in the
[Keep a Changelog](https://keepachangelog.com) format. calsync uses
[SemVer](https://semver.org).

## [Unreleased]

## [1.1.0] - 2026-11-12

### Added
- A calendar-feed source: a public calendar's iCal export (a Google Calendar
  public address, or any `.ics` URL) for schedules that are not fixtures. Each
  event is routed to a calendar by its category, which a person sets once per
  category on the source page, or skips. A new category goes to the source's
  default calendar and sends one push.
- Repeating series in these feeds are expanded inside the sync window,
  including monthly rules such as "first Wednesday" and single-occurrence
  overrides.

### Fixed
- The CalDAV target keeps `/` out of an event's resource name, which a server
  reads as a path.

## [1.0.0] - 2026-11-01

First public release.

### Added
- Sources: 360Player and TeamReach ICS feeds, and pushed sources over the
  inbound API.
- Destinations: CalDAV (Radicale bundled), a directory of `.ics` files, and
  Apple Calendar through the Mac mirror.
- The console: onboard a team, answer what a parse could not, correct events,
  and see what was written.
- The read API, with its contract at `GET /v1`.
- Guards that hold a poll where too much vanished or every id changed.
- `init-deploy`, which writes a complete stack from the image.
