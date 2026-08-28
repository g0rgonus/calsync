import XCTest
@testable import CalsyncMirrorCore

/// A truncated read is the dangerous one: it parses, and the events lost off
/// the end look exactly like cancellations.
final class TruncationTests: XCTestCase {

    func fixture() throws -> String {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "Fixtures/games", withExtension: "ics"))
        return try String(contentsOf: url, encoding: .utf8)
    }

    func testCompleteDocumentParses() throws {
        XCTAssertEqual(try ICS.parseCalendar(fixture()).count, 2)
    }

    func testTruncatedDocumentThrowsRatherThanLosingEvents() throws {
        let whole = try fixture()
        // Cut after the first event: valid prefix, one event, no END:VCALENDAR.
        let cut = whole.range(of: "END:VEVENT").map { whole[..<$0.upperBound] } ?? ""
        XCTAssertThrowsError(try ICS.parseCalendar(String(cut))) { error in
            guard case ICSError.truncated = error else {
                return XCTFail("expected .truncated, got \(error)")
            }
        }
    }

    /// The guard would catch a big truncation as a disappearance. It would wave
    /// a small one through, which is why completeness is checked in the parser.
    func testLosingASingleTrailingEventStillThrows() throws {
        let whole = try fixture()
        let cut = String(whole.dropLast("END:VCALENDAR\r\n".count))
        XCTAssertThrowsError(try ICS.parseCalendar(cut))
    }
}

final class HealthTests: XCTestCase {

    let now = Date(timeIntervalSince1970: 1_780_000_000)

    func testReachingResetsTheFailureRun() {
        var health = Health(
            lastReachedAt: now.addingTimeInterval(-7200),
            consecutiveFailures: 9,
            firstFailureAt: now.addingTimeInterval(-3600))
        health.recordReached(at: now)
        XCTAssertEqual(health.consecutiveFailures, 0)
        XCTAssertNil(health.firstFailureAt)
        XCTAssertEqual(health.lastReachedAt, now)
    }

    func testFirstFailureIsRememberedOnce() {
        var health = Health(lastReachedAt: now.addingTimeInterval(-3600))
        health.recordUnreachable(at: now)
        health.recordUnreachable(at: now.addingTimeInterval(900))
        XCTAssertEqual(health.consecutiveFailures, 2)
        XCTAssertEqual(health.firstFailureAt, now)
    }

    /// A trip away is unremarkable and gets one quiet line.
    func testRecentlyReachedIsQuiet() {
        var health = Health(lastReachedAt: now.addingTimeInterval(-3 * 3600))
        health.recordUnreachable(at: now)
        let report = health.report(now: now, warnAfterHours: 48)
        guard case .quiet(let message) = report else {
            return XCTFail("expected quiet, got \(report)")
        }
        XCTAssertTrue(message.contains("3h"), message)
    }

    /// Past the threshold, "I am away from home" stops being the explanation.
    func testLongSilenceWarns() {
        var health = Health(lastReachedAt: now.addingTimeInterval(-5 * 86400))
        health.recordUnreachable(at: now)
        let report = health.report(now: now, warnAfterHours: 48)
        guard case .warn(let message) = report else {
            return XCTFail("expected warn, got \(report)")
        }
        XCTAssertTrue(message.contains("5 days"), message)
    }

    /// Never having worked is not a trip away, so it says so early.
    func testNeverReachedWarnsFromTheSecondAttempt() {
        var health = Health()
        health.recordUnreachable(at: now)
        if case .warn = health.report(now: now, warnAfterHours: 48) {
            XCTFail("one failure should stay quiet")
        }
        health.recordUnreachable(at: now.addingTimeInterval(900))
        guard case .warn(let message) = health.report(now: now, warnAfterHours: 48) else {
            return XCTFail("expected warn on the second attempt")
        }
        XCTAssertTrue(message.contains("never"), message)
    }

    /// Losing the file must cost nothing but the memory of being offline.
    func testMissingFileLoadsAsEmpty() {
        let missing = URL(fileURLWithPath: "/nonexistent/calsync-mirror/health.json")
        XCTAssertEqual(Health.load(from: missing), Health())
    }

    func testCorruptFileLoadsAsEmpty() throws {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("calsync-health-\(UUID().uuidString).json")
        try Data("{ not json".utf8).write(to: path)
        defer { try? FileManager.default.removeItem(at: path) }
        XCTAssertEqual(Health.load(from: path), Health())
    }

    func testRoundTrip() throws {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("calsync-health-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: path) }
        var health = Health()
        health.recordReached(at: now)
        health.save(to: path)
        let reloaded = try XCTUnwrap(Health.load(from: path).lastReachedAt)
        XCTAssertEqual(reloaded.timeIntervalSince1970,
                       now.timeIntervalSince1970, accuracy: 0.001)
    }
}

final class ConfigCompatibilityTests: XCTestCase {

    /// A config written before these fields existed must keep loading. A tool
    /// that refuses to start because it gained a setting stops syncing on
    /// upgrade, which is the failure it was meant to prevent.
    func testOlderConfigStillLoads() throws {
        let json = """
            {"radicaleURL": "http://box:5232/calsync",
             "pairs": [{"collection": "games", "calendar": "Kid Activities"}]}
            """
        let config = try JSONDecoder().decode(Config.self, from: Data(json.utf8))
        XCTAssertEqual(config.timeoutSeconds, 10)
        XCTAssertEqual(config.offlineWarnAfterHours, 48)
        XCTAssertEqual(config.windowBackDays, 30)
        XCTAssertEqual(config.maxDisappearanceCount, 3)
        XCTAssertNil(config.username)
    }

    func testExplicitValuesWin() throws {
        let json = """
            {"radicaleURL": "http://box:5232/calsync", "pairs": [],
             "timeoutSeconds": 3, "offlineWarnAfterHours": 6, "maxDisappearanceCount": 1}
            """
        let config = try JSONDecoder().decode(Config.self, from: Data(json.utf8))
        XCTAssertEqual(config.timeoutSeconds, 3)
        XCTAssertEqual(config.offlineWarnAfterHours, 6)
        XCTAssertEqual(config.policy.maxCount, 1)
    }
}

final class ReachabilityTests: XCTestCase {

    /// Off-site classification drives whether a run is silent or an error, so
    /// the mapping is asserted rather than assumed.
    func testOfflineCodesAreTreatedAsUnreachable() {
        for code in RadicaleClient.offlineCodes {
            let error = RadicaleError.unreachable(url: "http://x/", reason: "\(code)")
            XCTAssertTrue(error.isUnreachable)
        }
    }

    func testCaptivePortalCountsAsUnreachable() {
        let portal = RadicaleError.notCalendar(
            url: "http://box:5232/calsync/games/",
            detail: "no BEGIN:VCALENDAR", looksLikeHTML: true)
        XCTAssertTrue(portal.isUnreachable, "an HTML page on an unknown network is a portal")
        XCTAssertTrue(portal.description.lowercased().contains("captive portal"))
    }

    /// A real fault must not be filed as "you are probably off-site".
    func testRealFaultsAreNotUnreachable() {
        XCTAssertFalse(
            RadicaleError.http(status: 404, url: "http://x/", body: "").isUnreachable)
        XCTAssertFalse(
            RadicaleError.http(status: 401, url: "http://x/", body: "").isUnreachable)
        XCTAssertFalse(RadicaleError.badURL("nope").isUnreachable)
        XCTAssertFalse(
            RadicaleError.notCalendar(url: "http://x/", detail: "truncated",
                                      looksLikeHTML: false).isUnreachable)
    }

    func testTimeoutIsAnOfflineCode() {
        XCTAssertTrue(RadicaleClient.offlineCodes.contains(.timedOut))
        XCTAssertTrue(RadicaleClient.offlineCodes.contains(.cannotConnectToHost))
    }
}

final class ReaderTests: XCTestCase {

    let pairs = [
        Pair(collection: "games", calendar: "A"),
        Pair(collection: "practices", calendar: "B"),
        Pair(collection: "extra", calendar: "C"),
    ]

    func testReadsEveryCollectionWhenAllIsWell() async {
        var asked: [String] = []
        let reads = await Reader.readAll(pairs: pairs) { name in
            asked.append(name)
            return []
        }
        XCTAssertEqual(asked, ["games", "practices", "extra"])
        XCTAssertTrue(reads.allSatisfy { $0.error == nil })
    }

    /// Every pair is the same server. Once it has timed out, trying the rest
    /// just spends another timeout each to learn the same thing.
    func testStopsAskingOnceTheServerIsUnreachable() async {
        var asked: [String] = []
        let reads = await Reader.readAll(pairs: pairs) { name in
            asked.append(name)
            throw RadicaleError.unreachable(url: "http://box/", reason: "timed out")
        }
        XCTAssertEqual(asked, ["games"], "should not probe a host that just timed out")
        XCTAssertEqual(reads.count, 3)
        XCTAssertTrue(reads.allSatisfy { $0.isUnreachable })
    }

    /// A 404 on one collection says nothing about the next. Short-circuiting
    /// here would let one mistyped name silently stop the other calendar.
    func testAFaultOnOneCollectionDoesNotSuppressTheRest() async {
        var asked: [String] = []
        let reads = await Reader.readAll(pairs: pairs) { name in
            asked.append(name)
            if name == "games" {
                throw RadicaleError.http(status: 404, url: "http://box/games/", body: "")
            }
            return []
        }
        XCTAssertEqual(asked, ["games", "practices", "extra"])
        XCTAssertFalse(reads[0].isUnreachable)
        XCTAssertNil(reads[1].error)
        XCTAssertNil(reads[2].error)
    }

    func testUnreachableAfterAGoodReadStillStopsTheRest() async {
        var asked: [String] = []
        _ = await Reader.readAll(pairs: pairs) { name in
            asked.append(name)
            if name == "practices" {
                throw RadicaleError.unreachable(url: "http://box/", reason: "lost")
            }
            return []
        }
        XCTAssertEqual(asked, ["games", "practices"])
    }
}
