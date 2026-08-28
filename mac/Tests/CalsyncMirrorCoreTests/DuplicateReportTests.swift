import XCTest
@testable import CalsyncMirrorCore

/// Measured against a real calendar, the blocking key alone produced 187
/// warnings for 48 events — four to one — because the destination is a *kid
/// logistics* calendar where "same day" means drop-off, a swim practice and two
/// pickups. These hold the two fixes that made it readable.
final class DuplicateReportTests: XCTestCase {

    let now = Date(timeIntervalSince1970: 1_780_000_000)

    func desired(_ uid: String, at start: Date, title: String) -> ParsedEvent {
        ParsedEvent(uid: uid, calsyncUID: uid, summary: title,
                    start: start, end: start.addingTimeInterval(5400))
    }

    func existing(_ title: String, at start: Date, allDay: Bool = false) -> ExistingEvent {
        ExistingEvent(identifier: "ek-\(title)", title: title, start: start,
                      end: start.addingTimeInterval(3600), isAllDay: allDay, notes: nil)
    }

    /// The real legacy copy: same time, same thing.
    func testAnOverlappingHandMadeCopyIsFlagged() {
        let at = now.addingTimeInterval(86400)
        let plan = Reconcile.plan(
            desired: [desired("a", at: at, title: "James ⚽️ U10DA Practice")],
            existing: [existing("James ⚽️ Practice", at: at)],
            now: now)
        XCTAssertEqual(plan.duplicates.count, 1)
    }

    /// Arrival time, not start time — why the band is an hour and not minutes.
    func testAnEventFortyFiveMinutesEarlyStillCounts() {
        let at = now.addingTimeInterval(86400)
        let plan = Reconcile.plan(
            desired: [desired("a", at: at, title: "James ⚽️ Practice")],
            existing: [existing("James ⚽️ Practice", at: at.addingTimeInterval(-2700))],
            now: now)
        XCTAssertEqual(plan.duplicates.count, 1)
    }

    /// School drop-off is not a possible duplicate of a soccer practice, and
    /// saying so is what taught somebody to scroll past the report.
    func testSomethingElseTheSameDayIsNotFlagged() {
        let practice = now.addingTimeInterval(86400)
        let dropOff = practice.addingTimeInterval(-9 * 3600)
        let plan = Reconcile.plan(
            desired: [desired("a", at: practice, title: "James ⚽️ U10DA Practice")],
            existing: [existing("School Drop-Off", at: dropOff)],
            now: now)
        XCTAssertTrue(plan.duplicates.isEmpty)
    }

    /// An all-day event carries no time to compare, so it falls back to the day
    /// rather than being silently dropped.
    func testAnAllDayEventFallsBackToTheDay() {
        let at = now.addingTimeInterval(86400)
        let plan = Reconcile.plan(
            desired: [desired("a", at: at, title: "James ⚽️ Practice")],
            existing: [existing("No School", at: at.addingTimeInterval(-9 * 3600),
                                allDay: true)],
            now: now)
        XCTAssertEqual(plan.duplicates.count, 1)
    }

    // --- the report ---------------------------------------------------------

    func testTheReportGroupsByWhatIsAlreadyThere() {
        var plan = MirrorPlan()
        let at = now.addingTimeInterval(86400)
        plan.creates = [PlannedCreate(
            uid: "a",
            fields: Reconcile.fields(for: desired("a", at: at, title: "James ⚽️ Practice")))]
        plan.duplicates =
            (0..<38).map { _ in
                DuplicateWarning(desiredTitle: "James ⚽️ U10DA Practice",
                                 desiredStart: at, existingTitle: "James ⚽️ Practice ",
                                 existingIdentifier: "x")
            }
            + (0..<34).map { _ in
                DuplicateWarning(desiredTitle: "James ⚽️ U10DA Practice",
                                 desiredStart: at, existingTitle: "Patrick 🏃‍♂️ Drylands",
                                 existingIdentifier: "y")
            }

        let lines = PlanReport.describe(plan)
        let grouped = lines.filter { $0.contains("×") }
        XCTAssertEqual(grouped.count, 2, "72 warnings must not be 72 lines")
        XCTAssertTrue(grouped[0].contains("38"), grouped[0])
        XCTAssertTrue(grouped[0].contains("James"), grouped[0])
        XCTAssertTrue(grouped[1].contains("34"), grouped[1])
        // Trailing space trimmed, or the counts read as different entries.
        XCTAssertFalse(grouped[0].hasSuffix(" "), grouped[0])
    }

    func testTheReportSaysNothingWasDeleted() {
        var plan = MirrorPlan()
        plan.duplicates = [DuplicateWarning(
            desiredTitle: "x", desiredStart: now,
            existingTitle: "y", existingIdentifier: "z")]
        XCTAssertTrue(PlanReport.describe(plan).contains { $0.contains("Nothing was deleted") })
    }

    func testAQuietPlanSaysSo() {
        XCTAssertTrue(PlanReport.describe(MirrorPlan()).contains { $0.contains("nothing to do") })
    }

    func testAHeldPlanSaysWhyAndThatNothingWent() {
        var plan = MirrorPlan()
        plan.hold = .disappearance(missing: 6, tracked: 21)
        let lines = PlanReport.describe(plan)
        XCTAssertTrue(lines.contains { $0.contains("HELD") })
        XCTAssertTrue(lines.contains { $0.contains("Nothing was deleted") })
    }
}
