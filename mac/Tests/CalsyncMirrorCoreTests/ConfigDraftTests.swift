import XCTest
@testable import CalsyncMirrorCore

/// The settings window is untested and the rules it enforces are not, which is
/// the same split the rest of this package is built on: the layer that can
/// delete things from a family calendar — and the form that configures it — is
/// a pure function over plain values.
final class ConfigDraftTests: XCTestCase {

    func draft(_ change: (inout ConfigDraft) -> Void = { _ in }) -> ConfigDraft {
        var draft = ConfigDraft(Config.sample)
        change(&draft)
        return draft
    }

    func problems(_ draft: ConfigDraft) -> [ConfigProblem] {
        do {
            _ = try draft.resolve()
            return []
        } catch let refusal as ConfigRefused {
            return refusal.problems
        } catch {
            XCTFail("unexpected error \(error)")
            return []
        }
    }

    func fields(_ draft: ConfigDraft) -> [ConfigField] {
        problems(draft).map(\.field)
    }

    // MARK: - Round trip

    /// Opening the window and pressing Save without touching anything must
    /// write back what was already there. A form that reformats a value on the
    /// way through is one that changes settings nobody edited.
    func testAnUntouchedDraftResolvesToTheSameConfig() throws {
        let config = Config(
            radicaleURL: "http://homebox:5232/calsync",
            username: "calsync", password: "hunter2",
            pairs: [Pair(collection: "games", calendar: "Kid Activities")],
            windowBackDays: 14, windowForwardDays: 200,
            maxDisappearancePct: 0.15, maxDisappearanceCount: 2,
            timeoutSeconds: 7.5, offlineWarnAfterHours: 36,
            syncIntervalMinutes: 20,
            apiURL: "https://homebox/v1", apiToken: "t0ken")
        XCTAssertEqual(try ConfigDraft(config).resolve(), config)
    }

    func testTheSampleResolves() throws {
        XCTAssertEqual(try ConfigDraft(Config.sample).resolve(), Config.sample)
    }

    /// A cleared box means "no opinion", the same tolerance `Config.init(from:)`
    /// extends to a file written by an older build.
    func testBlankNumbersFallBackToTheDefaults() throws {
        let resolved = try draft {
            $0.syncIntervalMinutes = ""
            $0.timeoutSeconds = ""
            $0.offlineWarnAfterHours = ""
            $0.maxDisappearancePct = ""
            $0.maxDisappearanceCount = ""
            $0.windowBackDays = ""
            $0.windowForwardDays = ""
        }.resolve()

        let defaults = Config(radicaleURL: "", pairs: [])
        XCTAssertEqual(resolved.syncIntervalMinutes, defaults.syncIntervalMinutes)
        XCTAssertEqual(resolved.timeoutSeconds, defaults.timeoutSeconds)
        XCTAssertEqual(resolved.maxDisappearancePct, defaults.maxDisappearancePct)
        XCTAssertEqual(resolved.maxDisappearanceCount, defaults.maxDisappearanceCount)
        XCTAssertEqual(resolved.windowForwardDays, defaults.windowForwardDays)
    }

    /// `10.0` in a box somebody is about to edit reads as a mistake.
    func testWholeNumbersAreShownWithoutADecimalPoint() {
        let shown = ConfigDraft(Config.sample)
        XCTAssertEqual(shown.timeoutSeconds, "10")
        XCTAssertEqual(shown.offlineWarnAfterHours, "48")
        XCTAssertEqual(shown.maxDisappearancePct, "0.2")
    }

    // MARK: - The guard ceiling

    /// `web/app.py:LIMITS` refuses to widen the guard past 0.5 / 25, and this
    /// window is the same form with a different toolkit in front of it. A guard
    /// a settings window can switch off in two clicks is not a guard.
    func testWideningTheGuardIsRefused() {
        XCTAssertEqual(fields(draft { $0.maxDisappearancePct = "0.9" }),
                       [.maxDisappearancePct])
        XCTAssertEqual(fields(draft { $0.maxDisappearanceCount = "40" }),
                       [.maxDisappearanceCount])
    }

    func testNarrowingTheGuardIsFree() throws {
        let resolved = try draft {
            $0.maxDisappearancePct = "0.05"
            $0.maxDisappearanceCount = "1"
        }.resolve()
        XCTAssertEqual(resolved.maxDisappearancePct, 0.05)
        XCTAssertEqual(resolved.maxDisappearanceCount, 1)
        XCTAssertEqual(resolved.policy.maxCount, 1)
    }

    /// Same allowance the console makes: a box labelled with a % is one
    /// somebody types 20 into.
    func testTwentyMeansTwentyPercent() throws {
        XCTAssertEqual(
            try draft { $0.maxDisappearancePct = "20" }.resolve().maxDisappearancePct,
            0.20, accuracy: 0.0001)
    }

    /// And 20 having been read as 0.2, 90 must still be refused rather than
    /// arriving as a tidy-looking 0.9.
    func testNinetyPercentIsStillRefused() {
        XCTAssertEqual(fields(draft { $0.maxDisappearancePct = "90" }),
                       [.maxDisappearancePct])
    }

    func testZeroDisappearanceCountIsRefused() {
        // Zero would hold every deletion for ever, which is a tool that quietly
        // stops removing cancelled games rather than a stricter guard.
        XCTAssertEqual(fields(draft { $0.maxDisappearanceCount = "0" }),
                       [.maxDisappearanceCount])
    }

    func testSomethingThatIsNotANumberIsRefused() {
        XCTAssertEqual(fields(draft { $0.syncIntervalMinutes = "quarter-hourly" }),
                       [.syncIntervalMinutes])
        XCTAssertEqual(fields(draft { $0.timeoutSeconds = "ten" }), [.timeoutSeconds])
    }

    func testAZeroMinuteIntervalIsRefused() {
        XCTAssertEqual(fields(draft { $0.syncIntervalMinutes = "0" }),
                       [.syncIntervalMinutes])
    }

    // MARK: - Addresses

    func testRadicaleURLIsRequired() {
        XCTAssertEqual(fields(draft { $0.radicaleURL = "  " }), [.radicaleURL])
    }

    /// A mistyped Radicale URL reads as being off-site and says nothing, which
    /// is why it is checked in the one place somebody would go looking.
    func testAnAddressWithoutASchemeOrHostIsRefused() {
        XCTAssertEqual(fields(draft { $0.radicaleURL = "homebox:5232/calsync" }),
                       [.radicaleURL])
        XCTAssertEqual(fields(draft { $0.radicaleURL = "ftp://homebox/calsync" }),
                       [.radicaleURL])
        XCTAssertEqual(fields(draft { $0.radicaleURL = "the one in the cupboard" }),
                       [.radicaleURL])
    }

    func testTheApiIsOptionalAndItsAbsenceIsNotAProblem() throws {
        let resolved = try draft { $0.apiURL = ""; $0.apiToken = "" }.resolve()
        XCTAssertNil(resolved.apiURL)
        XCTAssertNil(resolved.apiToken)
        XCTAssertNil(resolved.consoleReviewURL, "no address means no badge, quietly")
    }

    /// Half of the badge is worse than none: an address with no token is a 401
    /// every interval, and a token with no address is never sent anywhere.
    func testHalfAnApiConfigurationIsRefused() {
        XCTAssertEqual(fields(draft { $0.apiURL = "https://homebox/v1"; $0.apiToken = "" }),
                       [.apiToken])
        XCTAssertEqual(fields(draft { $0.apiURL = ""; $0.apiToken = "t0ken" }),
                       [.apiURL])
    }

    func testAConfiguredApiGivesTheConsoleLink() throws {
        let resolved = try draft {
            $0.apiURL = "https://homebox/v1"
            $0.apiToken = "t0ken"
        }.resolve()
        XCTAssertEqual(resolved.consoleReviewURL?.absoluteString,
                       "https://homebox/review")
    }

    // MARK: - Pairs

    func testBlankRowsAreDroppedAndExactRepeatsFolded() {
        let rows = [
            Pair(collection: " games ", calendar: " Kid Activities "),
            Pair(collection: "games", calendar: "Kid Activities"),
            Pair(collection: "", calendar: ""),
        ]
        XCTAssertEqual(ConfigDraft.normalised(rows),
                       [Pair(collection: "games", calendar: "Kid Activities")])
    }

    func testOrderIsPreserved() {
        let rows = [
            Pair(collection: "practices", calendar: "Grandparents"),
            Pair(collection: "games", calendar: "Kid Activities"),
        ]
        XCTAssertEqual(ConfigDraft.normalised(rows).map(\.collection),
                       ["practices", "games"])
    }

    func testHalfARowIsRefused() {
        XCTAssertEqual(
            fields(draft { $0.pairs = [Pair(collection: "games", calendar: "")] }),
            [.pairs])
        XCTAssertEqual(
            fields(draft { $0.pairs = [Pair(collection: "", calendar: "Kid Activities")] }),
            [.pairs])
    }

    func testMirroringNothingIsRefused() {
        XCTAssertEqual(fields(draft { $0.pairs = [] }), [.pairs])
    }

    /// The holding pens exist to keep an event calsync could not place off the
    /// whole family's phones. A free-text collection box is the one place a
    /// person could put one back.
    func testAHoldingPenIsRefusedAsADestination() {
        for pen in ["enrichment", "onboarding", "Enrichment"] {
            let refusals = problems(draft {
                $0.pairs = [Pair(collection: pen, calendar: "Kid Activities")]
            })
            XCTAssertEqual(refusals.map(\.field), [.pairs], "\(pen) should be refused")
            XCTAssertTrue(refusals[0].message.contains("holding pen"))
        }
    }

    /// Two collections into one calendar is the failure the pair rules exist
    /// for: each run is planned against the whole destination calendar, so the
    /// other collection's events look like they vanished upstream and each run
    /// would take turns deleting the other's work.
    func testTwoCollectionsCannotShareOneCalendar() {
        let refusals = problems(draft {
            $0.pairs = [
                Pair(collection: "games", calendar: "Kid Activities"),
                Pair(collection: "practices", calendar: "Kid Activities"),
            ]
        })
        XCTAssertEqual(refusals.map(\.field), [.pairs])
        XCTAssertTrue(refusals[0].message.contains("Kid Activities"))
    }

    /// The other way round is fine, and is a real arrangement: the grandparents
    /// get the games too.
    func testOneCollectionMayFeedTwoCalendars() throws {
        let resolved = try draft {
            $0.pairs = [
                Pair(collection: "games", calendar: "Kid Activities"),
                Pair(collection: "games", calendar: "Grandparents"),
            ]
        }.resolve()
        XCTAssertEqual(resolved.pairs.count, 2)
    }

    // MARK: - Reporting

    /// Every reason at once. A form that refuses one field per attempt is a
    /// form somebody fights.
    func testEveryProblemIsReportedTogether() {
        let refusals = problems(draft {
            $0.radicaleURL = "homebox"
            $0.maxDisappearanceCount = "99"
            $0.pairs = []
        })
        XCTAssertEqual(Set(refusals.map(\.field)),
                       [.radicaleURL, .maxDisappearanceCount, .pairs])
        XCTAssertEqual(refusals.count, 3)
    }

    func testNothingIsWrittenWhenARefusalHappens() throws {
        // `resolve` is the only path to a `Config`, so a refusal cannot half-
        // apply: the caller never gets a value to write.
        XCTAssertThrowsError(try draft { $0.maxDisappearancePct = "1.0" }.resolve())
    }
}
