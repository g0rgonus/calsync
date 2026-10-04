import Foundation

/// What calsync says about each event it has written, from `GET /v1/placements`.
///
/// The mirror's guard exists because a broken read of Radicale looks exactly
/// like a cancelled season. But calsync *knows* when a season was cancelled —
/// it did the deleting — so an absence it accounts for is no evidence of a
/// broken read, and counting it made a person confirm on the Mac what they had
/// just confirmed in the console. The guard now measures only what nothing
/// accounts for.
///
/// No content, and nothing that could decide what an event *says*: a uid, the
/// collection it lives in, and whether it is off the calendar on purpose.
public struct Placement: Equatable, Decodable {
    public var uid: String
    public var collection: String
    /// `live`, or why it is not: `withheld`, `cancelled`, `approved`.
    public var state: String

    public init(uid: String, collection: String, state: String) {
        self.uid = uid
        self.collection = collection
        self.state = state
    }
}

public struct Placements: Equatable {
    var byUID: [String: Placement]

    public init(_ items: [Placement]) {
        byUID = Dictionary(items.map { ($0.uid, $0) }, uniquingKeysWith: { _, last in last })
    }

    /// Why this event is off the calendar, if calsync took it off — `nil`
    /// means nothing accounts for its absence, and the guard should count it.
    ///
    /// Only a removal accounts for anything. **A live event in another
    /// collection does not**, though a reclassification looks like that:
    /// so does a mistyped or swapped collection name in this machine's config,
    /// where every event in the calendar "belongs elsewhere" — and treating
    /// that as a move deleted the whole calendar in a dry run. The two cannot
    /// be told apart from here, so a reclassification stays the guard's
    /// business, as it always was. An event calsync has never heard of is
    /// unaccounted for too: forgetting is not a decision.
    public func reason(uid: String) -> String? {
        guard let placement = byUID[uid], placement.state != "live" else { return nil }
        return placement.state
    }

    struct Body: Decodable { var placements: [Placement] }

    public static func decode(_ data: Data) throws -> Placements {
        Placements(try JSONDecoder().decode(Body.self, from: data).placements)
    }
}

/// Reads `GET /v1/placements`.
///
/// Optional, like the review badge: with no API configured the mirror's guard
/// behaves exactly as it always has, and a held set is confirmed on the Mac.
public struct PlacementsClient {
    let endpoint: URL
    let token: String
    let session: URLSession

    public init?(config: Config, session: URLSession? = nil) {
        guard let base = config.apiURL, let token = config.apiToken,
              !base.isEmpty, !token.isEmpty, let url = URL(string: base)
        else { return nil }
        self.endpoint = url.appendingPathComponent("placements")
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

    public func fetch() async throws -> Placements {
        var request = URLRequest(url: endpoint)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw RadicaleError.http(
                status: http.statusCode, url: endpoint.absoluteString,
                body: String(data: data, encoding: .utf8) ?? "")
        }
        return try Placements.decode(data)
    }
}
