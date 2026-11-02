import Foundation

/// A deliberate hold on writing, set from the menu.
///
/// **It expires by default, and that is the safety property.** A pause you
/// forget is the family's calendar going quietly stale for weeks, which is the
/// same failure `persists_across_seasons` exists to prevent on the calsync side
/// — switching something off in July means noticing in September. An indefinite
/// pause is still offered, because "I am about to reorganise this calendar by
/// hand" is a real reason, but it is the option you have to choose rather than
/// the one you land on.
///
/// Resuming needs no catch-up: the reconciler is stateless and re-derives
/// everything from the calendar, so the next run simply syncs.
public struct Pause: Codable, Equatable {
    /// When the pause lifts. `nil` means not paused.
    public var until: Date?
    /// When it was set, so a long one can say how long it has been.
    public var since: Date?

    public init(until: Date? = nil, since: Date? = nil) {
        self.until = until
        self.since = since
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        until = try c.decodeIfPresent(Date.self, forKey: .until)
        since = try c.decodeIfPresent(Date.self, forKey: .since)
    }

    public enum Duration: String, CaseIterable {
        case anHour, fourHours, tomorrow, indefinitely

        public var label: String {
            switch self {
            case .anHour: return "Pause for 1 hour"
            case .fourHours: return "Pause for 4 hours"
            case .tomorrow: return "Pause until tomorrow"
            case .indefinitely: return "Pause until I resume"
            }
        }

        /// Tomorrow means 08:00 local, not "in 24 hours" — the point of pausing
        /// overnight is to have it back before anybody needs the calendar.
        public func expiry(from now: Date, calendar: Calendar = .current) -> Date {
            switch self {
            case .anHour: return now.addingTimeInterval(3600)
            case .fourHours: return now.addingTimeInterval(4 * 3600)
            case .tomorrow:
                let tomorrow = calendar.date(byAdding: .day, value: 1, to: now) ?? now
                return calendar.date(
                    bySettingHour: 8, minute: 0, second: 0, of: tomorrow)
                    ?? now.addingTimeInterval(86400)
            case .indefinitely: return .distantFuture
            }
        }
    }

    public static func starting(_ duration: Duration, at now: Date,
                                calendar: Calendar = .current) -> Pause {
        Pause(until: duration.expiry(from: now, calendar: calendar), since: now)
    }

    public static let none = Pause()

    public func isActive(at now: Date) -> Bool {
        guard let until else { return false }
        return now < until
    }

    /// How the menu should describe it.
    public func describe(at now: Date, formatter: DateFormatter) -> String {
        guard let until, isActive(at: now) else { return "" }
        if until == .distantFuture { return "Paused" }
        return "Paused until \(formatter.string(from: until))"
    }

    public static let defaultPath = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/calsync-mirror/pause.json")

    /// Never throws. A pause that cannot be read is not a pause — failing open
    /// keeps the calendar current, which is the behaviour somebody who never
    /// touched this file expects.
    public static func load(from url: URL = defaultPath) -> Pause {
        guard let data = try? Data(contentsOf: url),
              let pause = try? JSONDecoder().decode(Pause.self, from: data)
        else { return .none }
        return pause
    }

    public func save(to url: URL = defaultPath) {
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try? encoder.encode(self).write(to: url)
    }
}
