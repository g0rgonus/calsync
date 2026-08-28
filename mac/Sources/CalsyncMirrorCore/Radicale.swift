import Foundation

public enum RadicaleError: Error, CustomStringConvertible {
    case badURL(String)
    /// No route to Radicale from here. Off-site this is the normal state, not
    /// a fault, and it is kept apart from every other failure for that reason.
    case unreachable(url: String, reason: String)
    case http(status: Int, url: String, body: String)
    /// Something answered, and it was not a calendar. Off-site this is usually
    /// a captive portal.
    case notCalendar(url: String, detail: String, looksLikeHTML: Bool)
    case transport(String)

    /// Whether this means "this machine cannot see Radicale right now".
    ///
    /// The distinction is the whole point: a laptop at a coffee shop is not a
    /// broken deployment, and reporting it as one every fifteen minutes trains
    /// somebody to ignore a log that will one day say something real.
    public var isUnreachable: Bool {
        switch self {
        case .unreachable: return true
        // A captive portal is a network that is lying about what it is, which
        // is a reachability problem wearing a 200.
        case .notCalendar(_, _, let looksLikeHTML): return looksLikeHTML
        default: return false
        }
    }

    /// An error page is usually HTML, and pasting a whole one into the terminal
    /// buries the status code that actually says what went wrong.
    static func summarise(_ body: String) -> String {
        let collapsed = body
            .replacingOccurrences(of: "<[^>]+>", with: " ", options: .regularExpression)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return collapsed.count > 160 ? String(collapsed.prefix(160)) + "…" : collapsed
    }

    public var description: String {
        switch self {
        case .badURL(let s):
            return "not a usable URL: \(s)"
        case .unreachable(let url, let reason):
            return "cannot reach \(url): \(reason)"
        case .http(let status, let url, let body):
            let hint: String
            switch status {
            case 401, 403:
                hint = " — Radicale wants a credential, or this is the server root "
                    + "rather than the principal (collections live under a user)"
            case 404:
                hint = " — no such collection; check the name against what calsync writes"
            default:
                hint = ""
            }
            let detail = Self.summarise(body)
            return "GET \(url) returned \(status)\(hint)"
                + (detail.isEmpty ? "" : "\n  server said: \(detail)")
        case .notCalendar(let url, let detail, let looksLikeHTML):
            let hint = looksLikeHTML
                ? "\n  that is an HTML page, not a calendar — on an unfamiliar network "
                    + "this is usually a captive portal intercepting the request"
                : ""
            return "GET \(url) did not return a calendar\(hint)\n  \(detail)"
        case .transport(let s):
            return "could not reach Radicale: \(s)"
        }
    }
}

/// Reads a collection as one iCalendar document.
///
/// A plain `GET` on the collection URL, not PROPFIND or a calendar-query
/// REPORT: Radicale serves the whole collection as a single `.ics` on GET,
/// which is the same path a phone uses to subscribe. That removes the entire
/// CalDAV verb surface from this tool — it needs no `caldav` library, no XML,
/// and no multistatus parsing to do a job that is fundamentally "read a file".
///
/// Reading Radicale rather than calsync's `GET /v1/events` is deliberate. The
/// API serves the *receipt* — the feed's view of an event — so it carries no
/// composed description, no `"Venue Name, Street Address"` location, and no
/// alarm policy. A mirror that read it would have to re-implement `build_body`,
/// the location composition and the alarm rules in a second language, and all
/// three would drift from calsync the first time a template changed. Radicale
/// holds the literal VEVENT that calsync wrote, which is the thing being
/// mirrored.
public struct RadicaleClient {
    let base: URL
    let username: String?
    let password: String?
    let session: URLSession

    /// URLSession codes that mean "there is no route from here".
    ///
    /// `.timedOut` is in the list and is the one that matters most off-site: a
    /// private address on a network that is not this one does not refuse the
    /// connection, it swallows it.
    static let offlineCodes: Set<URLError.Code> = [
        .notConnectedToInternet, .cannotFindHost, .cannotConnectToHost,
        .timedOut, .networkConnectionLost, .dnsLookupFailed,
        .internationalRoamingOff, .dataNotAllowed,
        // A portal terminating TLS presents a certificate for somebody else.
        .secureConnectionFailed, .serverCertificateUntrusted,
    ]

    public init(config: Config, session: URLSession? = nil) throws {
        guard let url = URL(string: config.radicaleURL) else {
            throw RadicaleError.badURL(config.radicaleURL)
        }
        self.base = url
        self.username = config.username
        self.password = config.password
        if let session {
            self.session = session
        } else {
            // The default is 60 seconds, which off-site means this job sits
            // there for a minute per collection every fifteen minutes. There is
            // nothing to wait for: Radicale is on a LAN or a tailnet, so it
            // either answers quickly or it is not reachable at all.
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = config.timeoutSeconds
            configuration.timeoutIntervalForResource = config.timeoutSeconds * 2
            configuration.waitsForConnectivity = false
            self.session = URLSession(configuration: configuration)
        }
    }

    func url(for collection: String) -> URL {
        // The trailing slash is what distinguishes a collection from a
        // resource; without it Radicale redirects and some clients drop auth.
        base.appendingPathComponent(collection, isDirectory: true)
    }

    public func fetch(collection: String) async throws -> [ParsedEvent] {
        let target = url(for: collection)
        var request = URLRequest(url: target)
        request.httpMethod = "GET"
        request.setValue("text/calendar", forHTTPHeaderField: "Accept")
        if let username, let password,
           let token = "\(username):\(password)".data(using: .utf8)?.base64EncodedString() {
            request.setValue("Basic \(token)", forHTTPHeaderField: "Authorization")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError where Self.offlineCodes.contains(error.code) {
            throw RadicaleError.unreachable(
                url: target.absoluteString, reason: error.localizedDescription)
        } catch {
            throw RadicaleError.transport("\(target): \(error.localizedDescription)")
        }

        let body = String(data: data, encoding: .utf8) ?? ""
        if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw RadicaleError.http(
                status: http.statusCode, url: target.absoluteString, body: body)
        }

        do {
            return try ICS.parseCalendar(body)
        } catch let error as ICSError {
            let head = body.trimmingCharacters(in: .whitespacesAndNewlines).prefix(200)
            let looksLikeHTML = head.lowercased().hasPrefix("<!doctype")
                || head.lowercased().hasPrefix("<html")
                || (response as? HTTPURLResponse)?
                    .value(forHTTPHeaderField: "Content-Type")?
                    .lowercased().contains("html") == true
            throw RadicaleError.notCalendar(
                url: target.absoluteString,
                detail: error.description,
                looksLikeHTML: looksLikeHTML)
        }
    }
}
