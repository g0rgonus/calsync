import XCTest
@testable import CalsyncMirrorCore

final class MarkerTests: XCTestCase {

    func testRoundTrip() {
        let notes = Marker.notes(body: "Soccer · Otters\nStart 14:00 EDT", uid: "evt-1")
        XCTAssertEqual(Marker.uid(inNotes: notes), "evt-1")
        XCTAssertTrue(notes.hasPrefix("Soccer · Otters"))
        XCTAssertTrue(notes.contains(Marker.prefix))
    }

    func testEmptyBodyStillCarriesIdentity() {
        XCTAssertEqual(Marker.uid(inNotes: Marker.notes(body: nil, uid: "evt-2")), "evt-2")
        XCTAssertEqual(Marker.uid(inNotes: Marker.notes(body: "   ", uid: "evt-3")), "evt-3")
    }

    /// A uid with punctuation in it — calsync's real uids look like
    /// `360Player-event-4716716`.
    func testRealisticUID() {
        let notes = Marker.notes(body: "x", uid: "360Player-event-4716716")
        XCTAssertEqual(Marker.uid(inNotes: notes), "360Player-event-4716716")
    }

    func testHandTypedNotesAreNotOurs() {
        XCTAssertNil(Marker.uid(inNotes: nil))
        XCTAssertNil(Marker.uid(inNotes: "Nadia haircut"))
        XCTAssertNil(Marker.uid(inNotes: "Bring the good boots"))
        XCTAssertFalse(Marker.isManaged(notes: "Soccer game"))
    }
}

final class GuardTests: XCTestCase {
    let policy = DisappearanceGuard()

    func testNothingMissingProceeds() {
        XCTAssertNil(policy.evaluate(trackedFuture: 10, missing: 0, incoming: 10))
    }

    func testSmallDisappearanceProceeds() {
        XCTAssertNil(policy.evaluate(trackedFuture: 20, missing: 3, incoming: 17))
    }

    /// Matches `diff.MAX_DISAPPEARANCE_COUNT`: strictly more than three.
    func testOverCountHolds() {
        XCTAssertNotNil(policy.evaluate(trackedFuture: 40, missing: 4, incoming: 36))
    }

    /// Matches `diff.MAX_DISAPPEARANCE_PCT`: strictly more than 20%.
    func testOverPercentHolds() {
        XCTAssertNotNil(policy.evaluate(trackedFuture: 8, missing: 3, incoming: 5))
        XCTAssertNil(policy.evaluate(trackedFuture: 15, missing: 3, incoming: 12))
    }

    func testTotalTurnoverHolds() {
        let verdict = policy.evaluate(trackedFuture: 12, missing: 12, incoming: 12)
        XCTAssertEqual(verdict, .identityTurnover(tracked: 12, incoming: 12))
    }

    /// An empty read is a disappearance, not a turnover — and either way it is
    /// held. This is the 502-from-Radicale case.
    func testEmptyReadHolds() {
        XCTAssertNotNil(policy.evaluate(trackedFuture: 12, missing: 12, incoming: 0))
    }
}

final class ReconcileTests: XCTestCase {

    let now = Date(timeIntervalSince1970: 1_780_000_000)  // 2026-06-08 UTC

    func future(_ days: Int, minutes: Int = 0) -> Date {
        now.addingTimeInterval(Double(days) * 86400 + Double(minutes) * 60)
    }

    func desired(_ uid: String, at start: Date, title: String = "Game") -> ParsedEvent {
        ParsedEvent(
            uid: uid, calsyncUID: uid, summary: title,
            description: "body", location: "Thistledown Park",
            start: start, end: start.addingTimeInterval(5400))
    }

    func mirrored(_ event: ParsedEvent, identifier: String) -> ExistingEvent {
        let fields = Reconcile.fields(for: event)
        return ExistingEvent(
            identifier: identifier, title: fields.title, start: fields.start,
            end: fields.end, isAllDay: fields.isAllDay,
            location: fields.location, notes: fields.notes)
    }

    func testCreatesWhatIsNotThere() {
        let plan = Reconcile.plan(
            desired: [desired("a", at: future(3))], existing: [], now: now)
        XCTAssertEqual(plan.creates.map(\.uid), ["a"])
        XCTAssertTrue(plan.deletes.isEmpty)
    }

    func testRecognisesItsOwnWriteAsUnchanged() {
        let event = desired("a", at: future(3))
        let plan = Reconcile.plan(
            desired: [event], existing: [mirrored(event, identifier: "ek-1")], now: now)
        XCTAssertEqual(plan.unchanged, 1)
        XCTAssertTrue(plan.creates.isEmpty)
        XCTAssertTrue(plan.updates.isEmpty)
    }

    func testUpdatesWhenTheRenderChanges() {
        let before = desired("a", at: future(3), title: "Jesse ⚽️ vs Harbour FC")
        var after = before
        after.summary = "Jesse ⚽️ @ Harbour FC"
        let plan = Reconcile.plan(
            desired: [after], existing: [mirrored(before, identifier: "ek-1")], now: now)
        XCTAssertEqual(plan.updates.map(\.uid), ["a"])
        XCTAssertEqual(plan.updates.first?.existing.identifier, "ek-1")
    }

    func testDeletesItsOwnVanishedEvent() {
        let kept = (1...9).map { desired("k\($0)", at: future($0)) }
        let gone = desired("gone", at: future(20))
        var existing = kept.enumerated().map { mirrored($1, identifier: "ek-\($0)") }
        existing.append(mirrored(gone, identifier: "ek-gone"))

        let plan = Reconcile.plan(desired: kept, existing: existing, now: now)
        XCTAssertNil(plan.hold)
        XCTAssertEqual(plan.deletes.map(\.identifier), ["ek-gone"])
    }

    /// The invariant the whole tool rests on.
    func testNeverTouchesAHandCreatedEvent() {
        let haircut = ExistingEvent(
            identifier: "ek-haircut", title: "Nadia haircut",
            start: future(2), end: future(2, minutes: 45), notes: "Bring the card")
        let plan = Reconcile.plan(desired: [], existing: [haircut], now: now)
        XCTAssertTrue(plan.deletes.isEmpty)
        XCTAssertTrue(plan.updates.isEmpty)
        XCTAssertNil(plan.hold)
    }

    /// A game played in April is history. `retire.py` refuses to remove one and
    /// so does this — Radicale holds the only copy of a past season.
    func testNeverDeletesAPastEvent() {
        let past = desired("old", at: future(-40))
        let plan = Reconcile.plan(
            desired: [], existing: [mirrored(past, identifier: "ek-old")], now: now)
        XCTAssertTrue(plan.deletes.isEmpty)
        XCTAssertNil(plan.hold)
    }

    func testGuardWithholdsDeletionsWhenTooManyVanish() {
        let all = (1...10).map { desired("k\($0)", at: future($0)) }
        let existing = all.enumerated().map { mirrored($1, identifier: "ek-\($0)") }
        let plan = Reconcile.plan(
            desired: Array(all.prefix(5)), existing: existing, now: now)
        XCTAssertNotNil(plan.hold)
        XCTAssertTrue(plan.deletes.isEmpty, "a held guard must delete nothing")
    }

    /// Radicale answering with an empty collection must not empty the calendar.
    func testEmptyReadDeletesNothing() {
        let all = (1...10).map { desired("k\($0)", at: future($0)) }
        let existing = all.enumerated().map { mirrored($1, identifier: "ek-\($0)") }
        let plan = Reconcile.plan(desired: [], existing: existing, now: now)
        XCTAssertNotNil(plan.hold)
        XCTAssertTrue(plan.deletes.isEmpty)
    }

    func testCancelledEventIsNotMirrored() {
        var event = desired("a", at: future(3))
        event.cancelled = true
        let plan = Reconcile.plan(desired: [event], existing: [], now: now)
        XCTAssertTrue(plan.creates.isEmpty)
    }

    /// RFC 5545 DTEND on a DATE is exclusive; EventKit's endDate is inclusive.
    /// Copying it across unchanged makes every tournament day two days long.
    func testAllDayEndBecomesInclusive() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/New_York")!
        let day = calendar.date(from: DateComponents(year: 2026, month: 10, day: 3))!
        let next = calendar.date(from: DateComponents(year: 2026, month: 10, day: 4))!
        let event = ParsedEvent(
            uid: "t", summary: "Semifinal Games", start: day, end: next, isAllDay: true)

        let fields = Reconcile.fields(for: event, calendar: calendar)
        XCTAssertEqual(fields.start, day)
        XCTAssertEqual(fields.end, day, "a one-day event must not end on the 4th")
        XCTAssertNil(fields.alarmOffsetSeconds)
    }

    func testFlagsAPossibleDuplicateWithoutActingOnIt() {
        let manual = ExistingEvent(
            identifier: "ek-manual", title: "Soccer game",
            start: future(3, minutes: -45), end: future(3), notes: nil)
        let plan = Reconcile.plan(
            desired: [desired("a", at: future(3), title: "Jesse ⚽️ vs Harbour FC")],
            existing: [manual], now: now)

        XCTAssertEqual(plan.creates.count, 1, "the game is created regardless")
        XCTAssertEqual(plan.duplicates.count, 1)
        XCTAssertEqual(plan.duplicates.first?.existingTitle, "Soccer game")
        XCTAssertTrue(plan.deletes.isEmpty, "never auto-delete the hand-made one")
    }
}
