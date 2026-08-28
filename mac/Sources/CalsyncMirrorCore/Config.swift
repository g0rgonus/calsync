import Foundation

/// One Radicale collection mirrored into one Apple calendar.
public struct Pair: Codable, Equatable {
    public var collection: String
    public var calendar: String

    public init(collection: String, calendar: String) {
        self.collection = collection
        self.calendar = calendar
    }
}

public struct Config: Codable, Equatable {
    /// The principal, not the server root — `http://host:5232/calsync`.
    /// CalDAV collections live under a user, and Radicale answers 403 at the
    /// root (`targeting.build_target` gets this wrong-footed too).
    public var radicaleURL: String
    public var username: String?
    public var password: String?
    public var pairs: [Pair]
    /// Only `games` and `practices` belong here. `onboarding` and `enrichment`
    /// are holding pens — putting an event calsync could not classify in front
    /// of the whole family is precisely what they exist to prevent.
    public var windowBackDays: Int
    public var windowForwardDays: Int
    public var maxDisappearancePct: Double
    public var maxDisappearanceCount: Int
    /// Radicale is on a LAN or a tailnet: it answers quickly or it is not
    /// reachable. The 60-second default leaves a launchd job hanging for a
    /// minute per collection every fifteen minutes while off-site.
    public var timeoutSeconds: Double
    /// How long unreachable stays unremarkable. Being away for a weekend is
    /// normal; two days of silence is worth a louder line in the log.
    public var offlineWarnAfterHours: Double
    /// How often the menu bar app syncs. The CLI ignores this — its schedule is
    /// whatever invokes it.
    public var syncIntervalMinutes: Int
    /// calsync's read API, e.g. `https://homebox/v1`. Optional: without it the
    /// menu simply has no review badge, and the mirror is unaffected.
    public var apiURL: String?
    public var apiToken: String?

    public init(
        radicaleURL: String,
        username: String? = nil,
        password: String? = nil,
        pairs: [Pair],
        windowBackDays: Int = 30,
        windowForwardDays: Int = 365,
        maxDisappearancePct: Double = 0.20,
        maxDisappearanceCount: Int = 3,
        timeoutSeconds: Double = 10,
        offlineWarnAfterHours: Double = 48,
        syncIntervalMinutes: Int = 15,
        apiURL: String? = nil,
        apiToken: String? = nil
    ) {
        self.radicaleURL = radicaleURL
        self.username = username
        self.password = password
        self.pairs = pairs
        self.windowBackDays = windowBackDays
        self.windowForwardDays = windowForwardDays
        self.maxDisappearancePct = maxDisappearancePct
        self.maxDisappearanceCount = maxDisappearanceCount
        self.timeoutSeconds = timeoutSeconds
        self.offlineWarnAfterHours = offlineWarnAfterHours
        self.syncIntervalMinutes = syncIntervalMinutes
        self.apiURL = apiURL
        self.apiToken = apiToken
    }

    /// Everything but the URL and the pairs is optional on the way in, so a
    /// config written by an older build keeps loading after a field is added.
    /// A tool that refuses to start because it gained a setting is a tool that
    /// stops syncing on upgrade.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        radicaleURL = try c.decode(String.self, forKey: .radicaleURL)
        pairs = try c.decode([Pair].self, forKey: .pairs)
        username = try c.decodeIfPresent(String.self, forKey: .username)
        password = try c.decodeIfPresent(String.self, forKey: .password)
        windowBackDays = try c.decodeIfPresent(Int.self, forKey: .windowBackDays) ?? 30
        windowForwardDays = try c.decodeIfPresent(Int.self, forKey: .windowForwardDays) ?? 365
        maxDisappearancePct =
            try c.decodeIfPresent(Double.self, forKey: .maxDisappearancePct) ?? 0.20
        maxDisappearanceCount =
            try c.decodeIfPresent(Int.self, forKey: .maxDisappearanceCount) ?? 3
        timeoutSeconds = try c.decodeIfPresent(Double.self, forKey: .timeoutSeconds) ?? 10
        offlineWarnAfterHours =
            try c.decodeIfPresent(Double.self, forKey: .offlineWarnAfterHours) ?? 48
        syncIntervalMinutes =
            try c.decodeIfPresent(Int.self, forKey: .syncIntervalMinutes) ?? 15
        apiURL = try c.decodeIfPresent(String.self, forKey: .apiURL)
        apiToken = try c.decodeIfPresent(String.self, forKey: .apiToken)
    }

    /// Where the console lives, derived from the API URL.
    ///
    /// The proxy puts both on one origin — `/` is the console, `/v1` is the API
    /// (docs/deployment/proxy.md) — so knowing one is knowing the other, and
    /// there is no second thing to configure and keep in step.
    public var consoleReviewURL: URL? {
        guard let apiURL, let url = URL(string: apiURL) else { return nil }
        return url.deletingLastPathComponent().appendingPathComponent("review")
    }

    public static let defaultPath = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent(".config/calsync-mirror/config.json")

    public static let sample = Config(
        radicaleURL: "http://localhost:5232/calsync",
        username: nil,
        password: nil,
        pairs: [
            Pair(collection: "games", calendar: "Goergen Kid Activities"),
            Pair(collection: "practices", calendar: "Goergen Grandparents"),
        ]
    )

    public static func load(from url: URL) throws -> Config {
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(Config.self, from: data)
    }

    public func write(to url: URL) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(self).write(to: url)
        // The file may carry a calendar password, so it is never group- or
        // world-readable. Same posture as calsync's own secrets.json.
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    public var policy: DisappearanceGuard {
        DisappearanceGuard(maxPct: maxDisappearancePct, maxCount: maxDisappearanceCount)
    }
}
