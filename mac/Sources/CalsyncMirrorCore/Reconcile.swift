import Foundation

/// An event already in the destination calendar, reduced to what planning needs.
///
/// A plain struct rather than an `EKEvent` so the whole decision layer — which
/// is the part that can delete things — is testable without a calendar store,
/// a permission prompt, or a Mac.
public struct ExistingEvent: Equatable {
    public var identifier: String
    public var title: String
    public var start: Date
    public var end: Date
    public var isAllDay: Bool
    public var location: String?
    public var notes: String?

    public init(
        identifier: String, title: String, start: Date, end: Date,
        isAllDay: Bool = false, location: String? = nil, notes: String? = nil
    ) {
        self.identifier = identifier
        self.title = title
        self.start = start
        self.end = end
        self.isAllDay = isAllDay
        self.location = location
        self.notes = notes
    }

    /// Whose event this is. `nil` means a person made it, and this tool may
    /// never write to it or delete it.
    public var calsyncUID: String? { Marker.uid(inNotes: notes) }
}

/// What an EKEvent should look like once written.
public struct DesiredFields: Equatable {
    public var title: String
    public var start: Date
    public var end: Date
    public var isAllDay: Bool
    public var location: String?
    public var notes: String
    public var url: URL?
    public var alarmOffsetSeconds: Double?
    public var timeZoneID: String?
}

public struct PlannedCreate: Equatable {
    public var uid: String
    public var fields: DesiredFields
}

public struct PlannedUpdate: Equatable {
    public var uid: String
    public var existing: ExistingEvent
    public var fields: DesiredFields
}

/// An event about to be created that lands on the same day, in the same
/// calendar, as something a person entered.
///
/// `MATCHING.md` §2's blocking key **plus time proximity**, and nothing beyond
/// it. There is deliberately no scoring cascade and no auto-match: `PLAN.md`
/// §6a lists the adoption matcher as the first thing to cut, because clearing a
/// season of hand-typed entries takes ten minutes and a fuzzy matcher takes a
/// weekend to build and is used exactly once. This reports, so the cleanup is
/// guided rather than a hunt, and stops there.
///
/// The time window is not a refinement, it is what makes the report usable.
/// Measured against a real calendar, the blocking key alone produced 187
/// warnings for 48 events — four to one — because the destination is a *kid
/// logistics* calendar, and "same day" there means school drop-off, a swim
/// practice and two pickups. A report that names `School Drop-Off` as a
/// possible duplicate of a soccer practice is one somebody learns to scroll
/// past, which costs more than not having it.
///
/// The blocking key was never meant to stand alone in `MATCHING.md` either —
/// it is step one, and scoring follows. Time is the single highest-weighted
/// signal there (0.35, `1.0` within 15m), and its tier-2 tuple bands at ±60m
/// for the reason recorded in that document: hand-entered events frequently
/// encode *arrival* time rather than start time.
public struct DuplicateWarning: Equatable {
    public var desiredTitle: String
    public var desiredStart: Date
    public var existingTitle: String
    public var existingIdentifier: String
}

public struct MirrorPlan: Equatable {
    public var creates: [PlannedCreate] = []
    public var updates: [PlannedUpdate] = []
    public var unchanged: Int = 0
    public var deletes: [ExistingEvent] = []
    /// Set when the guard withheld deletions. `deletes` is empty when it is.
    public var hold: HoldReason? = nil
    public var duplicates: [DuplicateWarning] = []

    public var isEmpty: Bool {
        creates.isEmpty && updates.isEmpty && deletes.isEmpty
    }
}

public enum Reconcile {

    /// How close two events must start before one is worth mentioning as a
    /// possible copy of the other. `MATCHING.md`'s tier-2 band.
    public static let duplicateWindow: TimeInterval = 60 * 60

    /// EventKit stores to the second; a round-trip through iCloud can shift a
    /// value by less. Comparing exactly would rewrite every event on every run.
    static let dateTolerance: TimeInterval = 1.0

    /// Translate a parsed VEVENT into the fields EventKit should hold.
    ///
    /// The one conversion that matters is the all-day end. RFC 5545 DTEND on a
    /// DATE is **exclusive** — a single-day event ends on the following day —
    /// while EventKit's `endDate` for an all-day event is the last day
    /// *inclusive*. Copying the value across unchanged turns every tournament
    /// day into a two-day event.
    public static func fields(
        for event: ParsedEvent, calendar: Calendar = .current
    ) -> DesiredFields {
        var end = event.end
        if event.isAllDay {
            let inclusive = calendar.date(byAdding: .day, value: -1, to: event.end) ?? event.end
            end = inclusive < event.start ? event.start : inclusive
        }
        return DesiredFields(
            title: event.summary,
            start: event.start,
            end: end,
            isAllDay: event.isAllDay,
            location: event.location,
            notes: Marker.notes(body: event.description, uid: event.uid),
            url: event.url.flatMap(URL.init(string:)),
            // calsync already withholds an alarm on an all-day event, because
            // "90 minutes before" local midnight fires at 22:30 the evening
            // before for a time nobody knows yet. Belt and braces.
            alarmOffsetSeconds: event.isAllDay ? nil : event.alarmOffsetSeconds,
            timeZoneID: event.timeZoneID
        )
    }

    static func matches(_ existing: ExistingEvent, _ desired: DesiredFields) -> Bool {
        func sameDate(_ a: Date, _ b: Date) -> Bool {
            abs(a.timeIntervalSince(b)) < dateTolerance
        }
        func sameText(_ a: String?, _ b: String?) -> Bool {
            (a ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                == (b ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return existing.title == desired.title
            && sameDate(existing.start, desired.start)
            && sameDate(existing.end, desired.end)
            && existing.isAllDay == desired.isAllDay
            && sameText(existing.location, desired.location)
            && sameText(existing.notes, desired.notes)
    }

    /// Decide what to do, without doing any of it.
    ///
    /// - Parameters:
    ///   - desired: every VEVENT read from one Radicale collection.
    ///   - existing: every event currently in the destination calendar, within
    ///     the same window.
    ///   - now: events starting before this are never deletion candidates.
    public static func plan(
        desired: [ParsedEvent],
        existing: [ExistingEvent],
        now: Date,
        guardPolicy: DisappearanceGuard = DisappearanceGuard(),
        calendar: Calendar = .current
    ) -> MirrorPlan {
        var plan = MirrorPlan()

        // A cancelled VEVENT is a tombstone, not something to put on a phone.
        // calsync normally deletes outright, so this is the rarer path.
        let live = desired.filter { !$0.cancelled }

        var managed: [String: ExistingEvent] = [:]
        var unmanaged: [ExistingEvent] = []
        for item in existing {
            if let uid = item.calsyncUID {
                // A duplicate uid means a previous run wrote twice. Keep the
                // earliest-created (stable identifier order) and let the other
                // fall through to the deletion path as an orphan.
                if managed[uid] == nil { managed[uid] = item } else { unmanaged.append(item) }
            } else {
                unmanaged.append(item)
            }
        }

        var seen = Set<String>()
        for event in live {
            let fields = fields(for: event, calendar: calendar)
            seen.insert(event.uid)
            if let match = managed[event.uid] {
                if matches(match, fields) {
                    plan.unchanged += 1
                } else {
                    plan.updates.append(
                        PlannedUpdate(uid: event.uid, existing: match, fields: fields))
                }
            } else {
                plan.creates.append(PlannedCreate(uid: event.uid, fields: fields))
            }
        }

        // Deletion authority extends only to events carrying the marker, and
        // only forward. A game played in April is history: nothing upstream
        // will ever mention it again, and removing it would delete the record
        // of a season the kids played (`retire.py` refuses this too).
        let trackedFuture = managed.values.filter { $0.start >= now }
        let missing = trackedFuture
            .filter { !seen.contains($0.calsyncUID ?? "") }
            .sorted { $0.start < $1.start }

        if let hold = guardPolicy.evaluate(
            trackedFuture: trackedFuture.count, missing: missing.count, incoming: live.count
        ) {
            plan.hold = hold
        } else {
            plan.deletes = missing
        }

        // Reported only for events being created, since an update is already
        // ours. An all-day event carries no time to compare, so it falls back
        // to the day — the same allowance `MATCHING.md` makes when it scores an
        // all-day event flat rather than treating it as a miss.
        for create in plan.creates {
            for other in unmanaged where nearby(other, create.fields, calendar) {
                plan.duplicates.append(
                    DuplicateWarning(
                        desiredTitle: create.fields.title,
                        desiredStart: create.fields.start,
                        existingTitle: other.title,
                        existingIdentifier: other.identifier))
            }
        }

        return plan
    }

    static func nearby(
        _ existing: ExistingEvent, _ desired: DesiredFields, _ calendar: Calendar
    ) -> Bool {
        guard !existing.isAllDay, !desired.isAllDay else {
            return calendar.isDate(existing.start, inSameDayAs: desired.start)
        }
        return abs(existing.start.timeIntervalSince(desired.start)) <= duplicateWindow
    }
}
