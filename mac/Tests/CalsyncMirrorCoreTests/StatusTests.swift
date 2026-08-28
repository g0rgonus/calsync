import XCTest
@testable import CalsyncMirrorCore

final class PauseTests: XCTestCase {

    let now = Date(timeIntervalSince1970: 1_780_000_000)

    var formatter: DateFormatter {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }

    func testNotPausedByDefault() {
        XCTAssertFalse(Pause.none.isActive(at: now))
    }

    func testAnHourExpires() {
        let pause = Pause.starting(.anHour, at: now)
        XCTAssertTrue(pause.isActive(at: now.addingTimeInterval(1800)))
        XCTAssertFalse(pause.isActive(at: now.addingTimeInterval(3700)),
                       "a forgotten pause is the calendar going stale")
    }

    /// "Until tomorrow" means 08:00 local, not 24 hours from now — the point of
    /// pausing overnight is having it back before anybody needs the calendar.
    func testTomorrowMeansTheMorningNotTwentyFourHours() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/New_York")!
        let evening = calendar.date(
            from: DateComponents(year: 2026, month: 9, day: 12, hour: 21))!
        let pause = Pause.starting(.tomorrow, at: evening, calendar: calendar)
        let until = try? XCTUnwrap(pause.until)
        let parts = calendar.dateComponents([.day, .hour], from: until!)
        XCTAssertEqual(parts.day, 13)
        XCTAssertEqual(parts.hour, 8)
    }

    func testIndefiniteIsOfferedButDistinct() {
        let pause = Pause.starting(.indefinitely, at: now)
        XCTAssertTrue(pause.isActive(at: now.addingTimeInterval(86400 * 365)))
        XCTAssertEqual(pause.describe(at: now, formatter: formatter), "Paused")
    }

    func testRoundTripAndFailOpen() throws {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("pause-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: path) }
        Pause.starting(.anHour, at: now).save(to: path)
        XCTAssertTrue(Pause.load(from: path).isActive(at: now))

        try Data("{ broken".utf8).write(to: path)
        XCTAssertFalse(Pause.load(from: path).isActive(at: now),
                       "an unreadable pause must fail open, not freeze the calendar")
    }
}

final class ReviewCountsTests: XCTestCase {

    func testDecodesTheEndpoint() throws {
        let json = """
            {"held_events": 2, "answers_awaiting_decision": 1, "upstream_edits": 0,
             "needs_attention": 3, "sources": [], "enrichment_collection": "enrichment"}
            """
        let counts = try JSONDecoder().decode(ReviewCounts.self, from: Data(json.utf8))
        XCTAssertEqual(counts.heldEvents, 2)
        XCTAssertEqual(counts.answersAwaitingDecision, 1)
        XCTAssertEqual(counts.needsAttention, 3)
        XCTAssertFalse(counts.isQuiet)
    }

    /// A badge is not worth failing over: an older or newer server must not
    /// leave the menu blank.
    func testToleratesMissingFields() throws {
        let counts = try JSONDecoder().decode(
            ReviewCounts.self, from: Data(#"{"held_events": 4}"#.utf8))
        XCTAssertEqual(counts.heldEvents, 4)
        XCTAssertEqual(counts.needsAttention, 4, "summed when the server omits it")
    }

    func testSummaryNamesOnlyWhatIsWaiting() {
        XCTAssertEqual(ReviewCounts().summary, "Nothing waiting")
        XCTAssertEqual(
            ReviewCounts(heldEvents: 1, needsAttention: 1).summary, "1 event held")
        XCTAssertEqual(
            ReviewCounts(heldEvents: 3, answersAwaitingDecision: 2,
                         needsAttention: 5).summary,
            "3 events held, 2 answers to approve")
    }
}

final class StatusPresenterTests: XCTestCase {

    let now = Date(timeIntervalSince1970: 1_780_000_000)

    var formatter: DateFormatter {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }

    func status(
        _ outcome: SyncOutcome, pause: Pause = .none, health: Health = Health(),
        review: ReviewCounts? = nil
    ) -> MenuStatus {
        StatusPresenter.status(
            outcome: outcome, pause: pause, health: health, review: review,
            now: now, formatter: formatter)
    }

    /// The one state the user chose. Showing anything else makes the tool look
    /// broken when it is obeying an instruction.
    func testPausedOutranksEverything() {
        let paused = Pause.starting(.anHour, at: now)
        let result = status(
            .held("6 of 21 vanished", at: now), pause: paused,
            review: ReviewCounts(heldEvents: 9, needsAttention: 9))
        XCTAssertEqual(result.title, "Paused")
        XCTAssertFalse(result.needsAttention)
    }

    /// A pause left on for days is the failure this feature could cause.
    func testALongPauseSaysHowLongItHasBeen() {
        let stale = Pause(until: .distantFuture, since: now.addingTimeInterval(-3 * 86400))
        let result = status(.never, pause: stale)
        XCTAssertTrue(result.detail?.contains("3 days") == true, result.detail ?? "")
        XCTAssertTrue(result.detail?.contains("nothing has synced") == true)
    }

    /// Something odd happened to real data and nobody has looked. That outranks
    /// the tool merely not running.
    func testHeldOutranksOfflineAndNeedsAttention() {
        let result = status(.held("6 of 21 tracked events vanished", at: now))
        XCTAssertEqual(result.title, "Deletions withheld")
        XCTAssertTrue(result.needsAttention)
        XCTAssertTrue(result.detail?.contains("6 of 21") == true)
    }

    /// A trip away is not something to act on.
    func testRecentlyOfflineDoesNotDemandAttention() {
        let health = Health(lastReachedAt: now.addingTimeInterval(-3600))
        let result = status(.offline(at: now), health: health)
        XCTAssertEqual(result.title, "Radicale unreachable")
        XCTAssertFalse(result.needsAttention)
    }

    func testLongOfflineDoesDemandAttention() {
        let health = Health(lastReachedAt: now.addingTimeInterval(-6 * 86400))
        XCTAssertTrue(status(.offline(at: now), health: health).needsAttention)
    }

    func testAQuietSyncSaysSo() {
        let result = status(.ok(created: 0, updated: 0, deleted: 0, at: now))
        XCTAssertEqual(result.title, "In sync")
        XCTAssertFalse(result.needsAttention)
        XCTAssertTrue(result.detail?.contains("No changes") == true)
    }

    func testChangesAreCounted() {
        let result = status(
            .ok(created: 2, updated: 1, deleted: 0, at: now.addingTimeInterval(-120)))
        XCTAssertTrue(result.detail?.contains("2 added, 1 changed, 0 removed") == true,
                      result.detail ?? "")
    }

    /// calsync's work, surfaced but never urgent in the way a held delete is.
    func testReviewCountsShowWithoutRaisingAlarm() {
        let result = status(
            .ok(created: 0, updated: 0, deleted: 0, at: now),
            review: ReviewCounts(heldEvents: 3, needsAttention: 3))
        XCTAssertEqual(result.title, "3 events held")
        XCTAssertFalse(result.needsAttention, "not the mirror's problem to alarm about")
        XCTAssertNotEqual(result.symbol, "calendar")
    }

    func testNeverSyncedIsNotAFault() {
        let result = status(.never)
        XCTAssertEqual(result.title, "Not synced yet")
        XCTAssertFalse(result.needsAttention)
    }
}

final class DetailLinesTests: XCTestCase {

    /// A menu item does not wrap. One long line stretched the menu across the
    /// whole screen, which is how the calendar-not-found error rendered.
    func testALongLineIsCappedForTheMenu() {
        let long = "no calendar titled \"ZZ\". Available: 360Player Event calendar, "
            + "Birthdays, Birthdays, Calendar, Daniel, F1, Family, Goergen "
            + "Grandparents, Goergen Kid Activities, Home, TeamReach - Games"
        let status = MenuStatus(symbol: "x", title: "t", detail: long)
        let lines = status.detailLines()
        XCTAssertEqual(lines.count, 1)
        XCTAssertLessThanOrEqual(lines[0].count, 69)
        XCTAssertTrue(lines[0].hasSuffix("…"))
    }

    func testShortLinesAreLeftAlone() {
        let status = MenuStatus(symbol: "x", title: "t", detail: "No changes.")
        XCTAssertEqual(status.detailLines(), ["No changes."])
    }

    func testEachLineIsCappedIndependently() {
        let status = MenuStatus(
            symbol: "x", title: "t",
            detail: "short line\n" + String(repeating: "a", count: 200))
        let lines = status.detailLines()
        XCTAssertEqual(lines.count, 2)
        XCTAssertEqual(lines[0], "short line")
        XCTAssertLessThanOrEqual(lines[1].count, 69)
    }

    func testNoDetailIsNoLines() {
        XCTAssertEqual(MenuStatus(symbol: "x", title: "t").detailLines(), [])
        XCTAssertEqual(
            MenuStatus(symbol: "x", title: "t", detail: "\n\n").detailLines(), [])
    }

    /// Truncation should not stop mid-word when a boundary is close by.
    func testItBreaksOnAWordBoundaryWhenOneIsNear() {
        let text = String(repeating: "word ", count: 30)
        let line = MenuStatus(symbol: "x", title: "t", detail: text).detailLines()[0]
        XCTAssertFalse(line.dropLast().hasSuffix("wor"), line)
    }
}
