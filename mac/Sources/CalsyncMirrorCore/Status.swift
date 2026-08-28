import Foundation

/// What the last run did.
public enum SyncOutcome: Equatable {
    case never
    case ok(created: Int, updated: Int, deleted: Int, at: Date)
    case held(String, at: Date)
    case failed(String, at: Date)
    case offline(at: Date)

    public var at: Date? {
        switch self {
        case .never: return nil
        case .ok(_, _, _, let at), .held(_, let at),
             .failed(_, let at), .offline(let at):
            return at
        }
    }
}

/// Everything the menu bar shows, as data.
public struct MenuStatus: Equatable {
    /// SF Symbol name for the status item.
    public var symbol: String
    /// The headline, shown as the first (disabled) menu entry.
    public var title: String
    /// Supporting line, when there is one.
    public var detail: String?
    /// Whether this is a state somebody should act on. Drives the icon's
    /// emphasis, and whether a notification is worth posting.
    public var needsAttention: Bool

    public init(symbol: String, title: String, detail: String? = nil,
                needsAttention: Bool = false) {
        self.symbol = symbol
        self.title = title
        self.detail = detail
        self.needsAttention = needsAttention
    }
}

/// Turns state into what the menu says.
///
/// A pure function, deliberately: it is the part of the app worth testing, and
/// keeping it out of the AppDelegate means the states that matter — held,
/// offline, paused — have assertions rather than being verified by squinting at
/// a menu bar.
public enum StatusPresenter {

    /// Order matters, and this is the argument for it:
    ///
    /// - **Paused outranks everything.** It is the one state the user chose, and
    ///   showing anything else would make the tool look broken when it is
    ///   obeying an instruction.
    /// - **Held outranks failed and offline.** A withheld deletion means
    ///   something odd happened to real data and nobody has looked; an error or
    ///   an outage is the tool not running, which the next run may fix by
    ///   itself.
    /// - **Review counts come last**, because they are calsync's work rather
    ///   than the mirror's, and they are never urgent in the same way.
    public static func status(
        outcome: SyncOutcome,
        pause: Pause,
        health: Health,
        review: ReviewCounts?,
        now: Date,
        warnAfterHours: Double = 48,
        formatter: DateFormatter
    ) -> MenuStatus {
        let waiting = review.map { !$0.isQuiet } ?? false

        if pause.isActive(at: now) {
            var detail = pause.describe(at: now, formatter: formatter)
            // A long pause is the failure this feature could cause, so it says
            // so rather than sitting there looking deliberate.
            if let since = pause.since, now.timeIntervalSince(since) > 24 * 3600 {
                detail += " — paused \(Health.describe(now.timeIntervalSince(since)))"
                    + ", nothing has synced in that time"
            }
            return MenuStatus(
                symbol: "pause.circle", title: "Paused", detail: detail,
                needsAttention: false)
        }

        switch outcome {
        case .held(let reason, let at):
            return MenuStatus(
                symbol: "exclamationmark.triangle.fill",
                title: "Deletions withheld",
                detail: "\(reason)\nLast run \(ago(at, now: now)).",
                needsAttention: true)

        case .failed(let message, let at):
            return MenuStatus(
                symbol: "exclamationmark.triangle",
                title: "Last sync failed",
                detail: "\(message)\n\(ago(at, now: now)).",
                needsAttention: true)

        case .offline:
            let stale = health.lastReachedAt
                .map { now.timeIntervalSince($0) > warnAfterHours * 3600 } ?? true
            return MenuStatus(
                symbol: "wifi.slash",
                title: "Radicale unreachable",
                detail: health.report(now: now, warnAfterHours: warnAfterHours).message,
                needsAttention: stale)

        case .never:
            return MenuStatus(
                symbol: "calendar.badge.clock", title: "Not synced yet",
                detail: "Waiting for the first run.", needsAttention: false)

        case .ok(let created, let updated, let deleted, let at):
            let changed = created + updated + deleted
            let detail = changed == 0
                ? "No changes. Last checked \(ago(at, now: now))."
                : "\(created) added, \(updated) changed, \(deleted) removed "
                    + "\(ago(at, now: now))."
            return MenuStatus(
                symbol: waiting ? "calendar.badge.exclamationmark" : "calendar",
                title: waiting ? (review?.summary ?? "In sync") : "In sync",
                detail: detail,
                needsAttention: false)
        }
    }

    static func ago(_ then: Date, now: Date) -> String {
        let gap = now.timeIntervalSince(then)
        if gap < 60 { return "just now" }
        return "\(Health.describe(gap)) ago"
    }
}


extension MenuStatus {
    /// `detail`, split into lines short enough for a menu.
    ///
    /// An error carrying a list — "no calendar titled X. Available: …" — is the
    /// right thing to put in a log and the wrong thing to paste into a menu
    /// item, which does not wrap: one long line stretched the menu across the
    /// whole screen. The full text stays in the log, where the width is
    /// somebody else's problem.
    public func detailLines(limit: Int = 68) -> [String] {
        (detail ?? "")
            .split(separator: "\n")
            .map { line -> String in
                let text = line.trimmingCharacters(in: .whitespaces)
                guard text.count > limit else { return text }
                // Break on a word boundary when there is one near the end, so
                // a truncated sentence does not stop mid-word.
                let cut = String(text.prefix(limit))
                if let space = cut.lastIndex(of: " "),
                   cut.distance(from: cut.startIndex, to: space) > limit - 18 {
                    return String(cut[..<space]) + "…"
                }
                return cut + "…"
            }
            .filter { !$0.isEmpty }
    }
}
