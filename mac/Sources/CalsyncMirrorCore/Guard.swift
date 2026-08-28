import Foundation

/// Why a run withheld its deletions.
public enum HoldReason: Equatable {
    case disappearance(missing: Int, tracked: Int)
    case identityTurnover(tracked: Int, incoming: Int)

    public var message: String {
        switch self {
        case .disappearance(let missing, let tracked):
            let pct = tracked > 0 ? Double(missing) / Double(tracked) * 100 : 0
            return String(
                format: "%d of %d tracked future events (%.0f%%) vanished from "
                    + "Radicale in one run — holding all deletions pending confirmation",
                missing, tracked, pct)
        case .identityTurnover(let tracked, let incoming):
            return "none of \(tracked) tracked events matched any of \(incoming) "
                + "read from Radicale — holding everything pending confirmation"
        }
    }
}

/// The same protection `diff.py` applies to a feed, applied to Radicale.
///
/// Absence is the only cancellation signal here too: `CalDavTarget.cancel` is a
/// hard DELETE, so a cancelled game is simply not in the collection any more.
/// That makes a partial read, a 500, or a misconfigured collection name
/// indistinguishable from "the season was called off" — and this tool deletes
/// from a calendar four people read.
///
/// Thresholds match `diff.MAX_DISAPPEARANCE_*` deliberately. Two numbers that
/// mean the same thing drift apart the moment one of them is tuned.
public struct DisappearanceGuard {
    public var maxPct: Double
    public var maxCount: Int

    public init(maxPct: Double = 0.20, maxCount: Int = 3) {
        self.maxPct = maxPct
        self.maxCount = maxCount
    }

    /// `nil` means proceed.
    ///
    /// Only *future* tracked events count. A game last April leaving the
    /// window is routine, and counting it would trip the guard every month for
    /// no reason.
    public func evaluate(trackedFuture: Int, missing: Int, incoming: Int) -> HoldReason? {
        // Total turnover: nothing tracked survived and nothing arriving is
        // recognised. For a mirror that means the collection moved, the mapping
        // is wrong, or a different calendar answered — never a normal season.
        if trackedFuture > 0, incoming > 0, missing == trackedFuture {
            return .identityTurnover(tracked: trackedFuture, incoming: incoming)
        }
        guard missing > 0 else { return nil }
        let overCount = missing > maxCount
        let overPct = trackedFuture > 0
            && (Double(missing) / Double(trackedFuture)) > maxPct
        guard overCount || overPct else { return nil }
        return .disappearance(missing: missing, tracked: trackedFuture)
    }
}
