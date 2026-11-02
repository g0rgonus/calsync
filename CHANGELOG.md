# Changelog

All notable changes to calsync are recorded here, in the
[Keep a Changelog](https://keepachangelog.com) format. calsync uses
[SemVer](https://semver.org).

## [Unreleased]

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
