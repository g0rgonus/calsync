import Foundation

/// What calsync says is waiting on a human, from `GET /v1/review`.
///
/// Counts only. The questions, and answering them, are in the console — a menu
/// that rendered the questions would be one step from looking like it could
/// resolve them, and the review gate is structural (docs/API.md).
public struct ReviewCounts: Equatable, Decodable {
    public var heldEvents: Int
    public var answersAwaitingDecision: Int
    public var upstreamEdits: Int
    public var needsAttention: Int

    public init(heldEvents: Int = 0, answersAwaitingDecision: Int = 0,
                upstreamEdits: Int = 0, needsAttention: Int = 0) {
        self.heldEvents = heldEvents
        self.answersAwaitingDecision = answersAwaitingDecision
        self.upstreamEdits = upstreamEdits
        self.needsAttention = needsAttention
    }

    enum CodingKeys: String, CodingKey {
        case heldEvents = "held_events"
        case answersAwaitingDecision = "answers_awaiting_decision"
        case upstreamEdits = "upstream_edits"
        case needsAttention = "needs_attention"
    }

    /// Tolerant on the way in: a field this client has not heard of is not a
    /// reason to show nothing. The contract at `GET /v1` is the place to check
    /// what the server serves; a badge is not worth failing over.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        heldEvents = try c.decodeIfPresent(Int.self, forKey: .heldEvents) ?? 0
        answersAwaitingDecision =
            try c.decodeIfPresent(Int.self, forKey: .answersAwaitingDecision) ?? 0
        upstreamEdits = try c.decodeIfPresent(Int.self, forKey: .upstreamEdits) ?? 0
        needsAttention = try c.decodeIfPresent(Int.self, forKey: .needsAttention)
            ?? (heldEvents + answersAwaitingDecision + upstreamEdits)
    }

    public var isQuiet: Bool { needsAttention == 0 }

    /// One line for the menu, naming only the kinds that are non-zero.
    public var summary: String {
        guard !isQuiet else { return "Nothing waiting" }
        var parts: [String] = []
        if heldEvents > 0 {
            parts.append("\(heldEvents) event\(heldEvents == 1 ? "" : "s") held")
        }
        if answersAwaitingDecision > 0 {
            parts.append("\(answersAwaitingDecision) answer"
                + "\(answersAwaitingDecision == 1 ? "" : "s") to approve")
        }
        if upstreamEdits > 0 {
            parts.append("\(upstreamEdits) unexplained edit"
                + "\(upstreamEdits == 1 ? "" : "s")")
        }
        return parts.joined(separator: ", ")
    }
}

/// Reads calsync's review queue.
///
/// Optional by design: a deployment with no API configured simply has no badge,
/// and the mirror goes on mirroring. This is ambient information, and nothing
/// about writing the calendar depends on it.
public struct ReviewClient {
    let endpoint: URL
    let token: String
    let session: URLSession

    public init?(config: Config, session: URLSession? = nil) {
        guard let base = config.apiURL, let token = config.apiToken,
              !base.isEmpty, !token.isEmpty, let url = URL(string: base)
        else { return nil }
        self.endpoint = url.appendingPathComponent("review")
        self.token = token
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = config.timeoutSeconds
            configuration.waitsForConnectivity = false
            self.session = URLSession(configuration: configuration)
        }
    }

    public func fetch() async throws -> ReviewCounts {
        var request = URLRequest(url: endpoint)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw RadicaleError.http(
                status: http.statusCode, url: endpoint.absoluteString,
                body: String(data: data, encoding: .utf8) ?? "")
        }
        return try JSONDecoder().decode(ReviewCounts.self, from: data)
    }
}
