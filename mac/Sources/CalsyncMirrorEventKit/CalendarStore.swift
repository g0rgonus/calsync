import EventKit
import Foundation
import CalsyncMirrorCore

public enum StoreError: Error, CustomStringConvertible {
    case accessDenied
    case noCalendar(title: String, available: [String])
    case readOnly(title: String)
    case vanished(identifier: String)
    case save(title: String, underlying: String)

    public var description: String {
        switch self {
        case .accessDenied:
            return "calendar access denied — grant it in System Settings → "
                + "Privacy & Security → Calendars, then run this again"
        case .noCalendar(let title, let available):
            return "no calendar titled \(title.debugDescription).\nAvailable: "
                + available.sorted().joined(separator: ", ")
        case .readOnly(let title):
            return "calendar \(title.debugDescription) does not allow changes"
        case .vanished(let identifier):
            return "event \(identifier) disappeared between planning and writing"
        case .save(let title, let underlying):
            return "could not save \(title.debugDescription): \(underlying)"
        }
    }
}

/// The only part of this tool that touches EventKit.
///
/// Everything that *decides* anything lives in `CalsyncMirrorCore` and is
/// tested without a calendar store. This class does what it is told.
public final class CalendarStore {
    private let store = EKEventStore()

    public init() {}

    /// What the settings window may offer as a destination, and whether it is
    /// allowed to know.
    ///
    /// `granted` is carried alongside the titles rather than left implicit,
    /// because "no calendars" and "not allowed to look" are the same empty list
    /// and completely different situations. A picker that is empty because the
    /// permission dialog has not happened yet, with nothing saying so, is one
    /// somebody stares at.
    public struct CalendarChoices {
        public var titles: [String]
        public var granted: Bool

        public init(titles: [String], granted: Bool) {
            self.titles = titles
            self.granted = granted
        }
    }

    /// Whether EventKit will answer at all. Deliberately a *query*, not a
    /// request: opening a window is not a reason to put a permission dialog in
    /// front of somebody, and the design is that the first run which touches a
    /// calendar is the one that asks.
    public static var isAuthorized: Bool {
        EKEventStore.authorizationStatus(for: .event) == .fullAccess
    }

    /// Every calendar this tool could write to, by title.
    ///
    /// Only writable ones: a subscribed or holiday calendar is not a
    /// destination, and `calendar(titled:)` would refuse it later anyway — one
    /// refusal at save time beats a run that fails every fifteen minutes.
    ///
    /// Titles, because a title is the whole of what `Pair.calendar` holds and
    /// what `calendar(titled:)` matches on. Two calendars in different accounts
    /// can share one, in which case the store takes the first and this list
    /// shows it once; that ambiguity predates the picker and is not something a
    /// dropdown can resolve.
    public func choices() -> CalendarChoices {
        guard Self.isAuthorized else {
            return CalendarChoices(titles: [], granted: false)
        }
        let writable = store.calendars(for: .event)
            .filter(\.allowsContentModifications)
            .map(\.title)
        let unique = Array(Set(writable)).sorted {
            $0.localizedStandardCompare($1) == .orderedAscending
        }
        return CalendarChoices(titles: unique, granted: true)
    }

    public func requestAccess() async throws {
        let granted = try await store.requestFullAccessToEvents()
        guard granted else { throw StoreError.accessDenied }
    }

    public func calendar(titled title: String) throws -> EKCalendar {
        let all = store.calendars(for: .event)
        guard let match = all.first(where: { $0.title == title }) else {
            throw StoreError.noCalendar(title: title, available: all.map(\.title))
        }
        guard match.allowsContentModifications else {
            throw StoreError.readOnly(title: title)
        }
        return match
    }

    public func existing(in calendar: EKCalendar, from: Date, to: Date) -> [ExistingEvent] {
        let predicate = store.predicateForEvents(
            withStart: from, end: to, calendars: [calendar])
        return store.events(matching: predicate).compactMap { event in
            guard let identifier = event.eventIdentifier,
                  let start = event.startDate, let end = event.endDate
            else { return nil }
            return ExistingEvent(
                identifier: identifier,
                title: event.title ?? "",
                start: start,
                end: end,
                isAllDay: event.isAllDay,
                location: event.location,
                notes: event.notes)
        }
    }

    private func apply(_ fields: DesiredFields, to event: EKEvent) {
        event.title = fields.title
        event.startDate = fields.start
        event.endDate = fields.end
        // After the dates: EventKit snaps an event to day boundaries when this
        // is set, so setting it first and then assigning times undoes it.
        event.isAllDay = fields.isAllDay
        event.location = fields.location
        event.notes = fields.notes
        event.url = fields.url
        // An all-day event is floating by definition; pinning it to a zone is
        // how a tournament day shows up on the wrong date for a travelling parent.
        event.timeZone = fields.isAllDay
            ? nil
            : fields.timeZoneID.flatMap(TimeZone.init(identifier:))

        for alarm in event.alarms ?? [] { event.removeAlarm(alarm) }
        if let offset = fields.alarmOffsetSeconds {
            event.addAlarm(EKAlarm(relativeOffset: offset))
        }
    }

    public func create(_ planned: PlannedCreate, in calendar: EKCalendar) throws {
        let event = EKEvent(eventStore: store)
        event.calendar = calendar
        apply(planned.fields, to: event)
        do {
            try store.save(event, span: .thisEvent, commit: true)
        } catch {
            throw StoreError.save(
                title: planned.fields.title, underlying: error.localizedDescription)
        }
    }

    public func update(_ planned: PlannedUpdate) throws {
        guard let event = store.event(withIdentifier: planned.existing.identifier) else {
            throw StoreError.vanished(identifier: planned.existing.identifier)
        }
        apply(planned.fields, to: event)
        do {
            try store.save(event, span: .thisEvent, commit: true)
        } catch {
            throw StoreError.save(
                title: planned.fields.title, underlying: error.localizedDescription)
        }
    }

    /// Only ever called with an event whose notes carry the marker — the plan
    /// is what enforces that, and it is the invariant the whole tool rests on.
    public func delete(_ existing: ExistingEvent) throws {
        guard let event = store.event(withIdentifier: existing.identifier) else {
            // Already gone. Nothing to do and nothing to report.
            return
        }
        do {
            try store.remove(event, span: .thisEvent, commit: true)
        } catch {
            throw StoreError.save(
                title: existing.title, underlying: error.localizedDescription)
        }
    }
}
