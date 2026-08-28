import Foundation

/// The outcome of reading one collection.
public struct CollectionRead {
    public let pair: Pair
    public let events: [ParsedEvent]?
    public let error: Error?
    /// Whether the failure means "no route from here" rather than "something
    /// is wrong". Captured at construction because the caller's decisions —
    /// silence, exit code, health record — all turn on it.
    public let isUnreachable: Bool

    public init(pair: Pair, events: [ParsedEvent]?, error: Error?) {
        self.pair = pair
        self.events = events
        self.error = error
        self.isUnreachable = (error as? RadicaleError)?.isUnreachable ?? false
    }
}

public enum Reader {

    /// Read every collection, stopping early once the server proves unreachable.
    ///
    /// Every pair points at the same Radicale, so once one collection has timed
    /// out the rest will too — and each one costs another full timeout. Off-site
    /// that is the difference between one wasted interval and several, on a job
    /// that runs every fifteen minutes for as long as the trip lasts.
    ///
    /// Only an *unreachable* failure short-circuits. A 404 on one collection
    /// says nothing about the next one and must not suppress it, or a mistyped
    /// name would silently stop the other calendar syncing.
    public static func readAll(
        pairs: [Pair],
        fetch: (String) async throws -> [ParsedEvent]
    ) async -> [CollectionRead] {
        var reads: [CollectionRead] = []
        var unreachable: RadicaleError? = nil

        for pair in pairs {
            if let seen = unreachable {
                reads.append(CollectionRead(pair: pair, events: nil, error: seen))
                continue
            }
            do {
                reads.append(
                    CollectionRead(pair: pair, events: try await fetch(pair.collection),
                                   error: nil))
            } catch {
                if let radicale = error as? RadicaleError, radicale.isUnreachable {
                    unreachable = radicale
                }
                reads.append(CollectionRead(pair: pair, events: nil, error: error))
            }
        }
        return reads
    }
}
