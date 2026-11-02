import Foundation

/// How the run should talk about being offline.
public enum OfflineReport: Equatable {
    /// Normal: off-site, or Radicale restarting. One line, no alarm.
    case quiet(String)
    /// Long enough that "I am away from home" stops being the likely
    /// explanation.
    case warn(String)

    public var message: String {
        switch self {
        case .quiet(let m), .warn(let m): return m
        }
    }
}

/// When this machine could last see Radicale.
///
/// This is the tool's only persistent state, and it is deliberately of a
/// different kind from the state file the predecessor kept. That one held
/// *identity* — which calendar event corresponded to which source event — and
/// losing it meant recreating the whole calendar beside itself. This holds only
/// "when did a read last work", which is not derivable from anywhere else and
/// whose loss costs nothing: the worst case is that the tool forgets it has
/// been offline and starts counting again. Nothing about what gets written
/// depends on it.
public struct Health: Codable, Equatable {
    public var lastReachedAt: Date?
    public var consecutiveFailures: Int
    public var firstFailureAt: Date?

    public init(
        lastReachedAt: Date? = nil, consecutiveFailures: Int = 0,
        firstFailureAt: Date? = nil
    ) {
        self.lastReachedAt = lastReachedAt
        self.consecutiveFailures = consecutiveFailures
        self.firstFailureAt = firstFailureAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        lastReachedAt = try container.decodeIfPresent(Date.self, forKey: .lastReachedAt)
        consecutiveFailures =
            try container.decodeIfPresent(Int.self, forKey: .consecutiveFailures) ?? 0
        firstFailureAt = try container.decodeIfPresent(Date.self, forKey: .firstFailureAt)
    }

    public static let defaultPath = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/calsync-mirror/health.json")

    /// Never throws. A missing or corrupt health file must not stop a sync —
    /// it is a note about the past, not an input to any decision.
    public static func load(from url: URL = defaultPath) -> Health {
        guard let data = try? Data(contentsOf: url),
              let health = try? JSONDecoder().decode(Health.self, from: data)
        else { return Health() }
        return health
    }

    public func save(to url: URL = defaultPath) {
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try? encoder.encode(self).write(to: url)
    }

    public mutating func recordReached(at now: Date) {
        lastReachedAt = now
        consecutiveFailures = 0
        firstFailureAt = nil
    }

    public mutating func recordUnreachable(at now: Date) {
        consecutiveFailures += 1
        if firstFailureAt == nil { firstFailureAt = now }
    }

    /// What to print for a run that could not see Radicale at all.
    public func report(now: Date, warnAfterHours: Double) -> OfflineReport {
        let attempts = consecutiveFailures == 1
            ? "1 attempt" : "\(consecutiveFailures) attempts"

        guard let last = lastReachedAt else {
            // Never reached it. Off-site is not the explanation for a tool that
            // has never once worked, so this says so from the second attempt.
            let message = "Radicale has never been reachable from this machine "
                + "(\(attempts)) — check radicaleURL, and that you are on the "
                + "network or tailnet it lives on"
            return consecutiveFailures >= 2 ? .warn(message) : .quiet(message)
        }

        let gap = now.timeIntervalSince(last)
        let ago = Self.describe(gap)
        if gap > warnAfterHours * 3600 {
            return .warn(
                "Radicale unreachable for \(ago) (\(attempts)). If you are not "
                + "off-site, something is wrong — nothing has reached the "
                + "family's calendars in that time")
        }
        return .quiet("Radicale unreachable — last read \(ago) ago, nothing to do")
    }

    public static func describe(_ seconds: TimeInterval) -> String {
        let minutes = Int(seconds / 60)
        if minutes < 1 { return "under a minute" }
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        if hours < 48 { return "\(hours)h" }
        return "\(hours / 24) days"
    }
}
