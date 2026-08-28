import XCTest
@testable import CalsyncMirrorCore

/// The fixture is produced by calsync's own `targets/ics_file.py:to_ics`, not
/// hand-written here. A hand-written one tests the parser against the author's
/// belief about the format, which is the belief most likely to be wrong.
final class ICSTests: XCTestCase {

    func fixture() throws -> String {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "Fixtures/games", withExtension: "ics"))
        return try String(contentsOf: url, encoding: .utf8)
    }

    func zoned(_ y: Int, _ mo: Int, _ d: Int, _ h: Int, _ mi: Int) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/New_York")!
        return calendar.date(
            from: DateComponents(year: y, month: mo, day: d, hour: h, minute: mi))!
    }

    func testParsesBothEvents() throws {
        let events = try ICS.parseCalendar(fixture())
        XCTAssertEqual(events.count, 2)
    }

    func testTimedEvent() throws {
        let event = try XCTUnwrap(try ICS.parseCalendar(fixture()).first)
        XCTAssertEqual(event.uid, "360Player-event-4716716")
        XCTAssertEqual(event.calsyncUID, "360Player-event-4716716")
        XCTAssertEqual(event.summary, "Jesse ⚽️ vs Harbour FC")
        XCTAssertEqual(event.start, zoned(2026, 9, 12, 14, 0))
        XCTAssertEqual(event.end, zoned(2026, 9, 12, 15, 30))
        XCTAssertFalse(event.isAllDay)
        XCTAssertEqual(event.timeZoneID, "America/New_York")
        XCTAssertFalse(event.cancelled)
        XCTAssertEqual(event.sourceID, "p360-otters")
        XCTAssertEqual(event.activityID, "otters")
        XCTAssertEqual(event.contentHash, "abc123def456")
    }

    /// A colon appears in the value, and a naive split on the first one
    /// truncates it to "https".
    func testURLSurvivesTheColonSplit() throws {
        let event = try XCTUnwrap(try ICS.parseCalendar(fixture()).first)
        XCTAssertEqual(event.url, "https://app.360player.com/event/4716716")
    }

    /// calsync folds at 75 octets, and it folds mid-word. Unfolding must strip
    /// exactly the one inserted space — "Field:" + "  #2" is "Field: #2".
    func testUnfoldsDescriptionWithoutEatingContent() throws {
        let events = try ICS.parseCalendar(fixture())
        let description = try XCTUnwrap(events[0].description)
        XCTAssertTrue(description.contains("Field: #2"), description)
        XCTAssertTrue(description.contains("1009 Thistledown Rd, Marbury NX 40114"),
                      description)
        XCTAssertTrue(description.contains("Source: p360-otters"), description)
        // A fold in the second event lands inside "p360-otters".
        let second = try XCTUnwrap(events[1].description)
        XCTAssertTrue(second.contains("Source: p360-otters"), second)
    }

    func testUnescapesLocationCommas() throws {
        let event = try XCTUnwrap(try ICS.parseCalendar(fixture()).first)
        XCTAssertEqual(
            event.location, "Thistledown Park, 1009 Thistledown Rd, Marbury NX 40114")
    }

    /// 90 minutes, written as `-PT1H30M`. Assuming `-PT90M` parses nothing.
    func testAlarmDurationInHoursAndMinutes() throws {
        let event = try XCTUnwrap(try ICS.parseCalendar(fixture()).first)
        XCTAssertEqual(event.alarmOffsetSeconds, -5400)
    }

    func testAllDayEventKeepsExclusiveEnd() throws {
        let event = try ICS.parseCalendar(fixture())[1]
        XCTAssertTrue(event.isAllDay)
        XCTAssertNil(event.alarmOffsetSeconds)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        XCTAssertEqual(
            event.start, calendar.date(from: DateComponents(year: 2026, month: 10, day: 3)))
        XCTAssertEqual(
            event.end, calendar.date(from: DateComponents(year: 2026, month: 10, day: 4)))
    }

    func testDurationForms() {
        XCTAssertEqual(ICS.parseDuration("-PT90M"), -5400)
        XCTAssertEqual(ICS.parseDuration("-PT1H30M"), -5400)
        XCTAssertEqual(ICS.parseDuration("PT30M"), 1800)
        XCTAssertEqual(ICS.parseDuration("-P1D"), -86400)
        XCTAssertNil(ICS.parseDuration("nonsense"))
    }

    /// Zero events is a legitimate state — a season that has not started. It is
    /// the guard's job to decide whether that is suspicious, not the parser's.
    func testEmptyCalendarParsesRatherThanThrowing() throws {
        let empty = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//calsync//EN\r\nEND:VCALENDAR\r\n"
        XCTAssertEqual(try ICS.parseCalendar(empty).count, 0)
    }

    /// An HTML error page must not read as "everything was cancelled".
    func testNonCalendarBodyThrows() {
        XCTAssertThrowsError(try ICS.parseCalendar("<html><body>502 Bad Gateway</body></html>"))
    }

    func testCancelledStatusIsRead() throws {
        let doc = try fixture().replacingOccurrences(
            of: "STATUS:CONFIRMED", with: "STATUS:CANCELLED")
        XCTAssertTrue(try ICS.parseCalendar(doc).allSatisfy { $0.cancelled })
    }
}
