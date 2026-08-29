import Foundation

/// The bounds a settings form may not widen past.
///
/// These are `web/app.py:LIMITS`, held to the same numbers on purpose. The
/// console already refuses to widen `max_disappearance_pct` past 0.5 or the
/// count past 25, for the reason recorded there: a guard a web form can switch
/// off in two clicks is not a guard, and the invariant is never to raise a
/// threshold to make something pass. A settings window is the same form with a
/// different toolkit in front of it, and the mirror deletes from a calendar
/// four people read — so it gets the same ceiling rather than a friendlier one.
///
/// Narrowing is free in both directions of that argument: 0.10 and 1 are
/// stricter than the default and nothing here objects.
public enum ConfigLimits {
    public static let disappearancePct: ClosedRange<Double> = 0.01...0.5
    public static let disappearanceCount: ClosedRange<Int> = 1...25
    public static let windowBackDays: ClosedRange<Int> = 0...365
    public static let windowForwardDays: ClosedRange<Int> = 1...1095

    /// The mirror's own three, which the console has no equivalent of. They are
    /// bounded for a weaker reason — nothing here can wipe a calendar — but a
    /// zero-minute interval is a busy loop and a zero-second timeout is a tool
    /// that is permanently off-site, and both are one mistyped keystroke away.
    public static let syncIntervalMinutes: ClosedRange<Int> = 1...1440
    public static let timeoutSeconds: ClosedRange<Double> = 1...120
    public static let offlineWarnAfterHours: ClosedRange<Double> = 1...(24 * 90)
}

/// Which field a refusal is about, so the window can say it next to the box
/// rather than in one heap at the bottom.
public enum ConfigField: String, Equatable {
    case radicaleURL
    case username
    case password
    case apiURL
    case apiToken
    case syncIntervalMinutes
    case timeoutSeconds
    case offlineWarnAfterHours
    case maxDisappearancePct
    case maxDisappearanceCount
    case windowBackDays
    case windowForwardDays
    case pairs
}

public struct ConfigProblem: Equatable {
    public var field: ConfigField
    public var message: String

    public init(field: ConfigField, message: String) {
        self.field = field
        self.message = message
    }
}

/// Nothing was saved, and here is every reason why.
///
/// Every problem at once, not the first one: a form that refuses one field per
/// attempt is a form somebody fights, and the fields here are entered in one
/// sitting.
public struct ConfigRefused: Error, Equatable, CustomStringConvertible {
    public var problems: [ConfigProblem]

    public init(_ problems: [ConfigProblem]) { self.problems = problems }

    public var description: String {
        problems.map(\.message).joined(separator: "\n")
    }
}

/// What a settings form holds before it is a `Config`: strings, exactly as
/// typed, plus whatever rows the pair editor is showing.
///
/// It lives in Core rather than beside the window because the rules it enforces
/// are the interesting part — the guard ceiling, the holding pens, two pairs
/// aimed at one calendar — and every safety rule in this tool has a test that
/// needs no Mac and no permission prompt. The window is a way to fill this in.
public struct ConfigDraft: Equatable {
    public var radicaleURL: String = ""
    public var username: String = ""
    public var password: String = ""
    public var apiURL: String = ""
    public var apiToken: String = ""
    public var syncIntervalMinutes: String = ""
    public var timeoutSeconds: String = ""
    public var offlineWarnAfterHours: String = ""
    public var maxDisappearancePct: String = ""
    public var maxDisappearanceCount: String = ""
    public var windowBackDays: String = ""
    public var windowForwardDays: String = ""
    public var pairs: [Pair] = []

    public init() {}

    /// The window's starting state: what is on disk, as text.
    public init(_ config: Config) {
        radicaleURL = config.radicaleURL
        username = config.username ?? ""
        password = config.password ?? ""
        apiURL = config.apiURL ?? ""
        apiToken = config.apiToken ?? ""
        syncIntervalMinutes = String(config.syncIntervalMinutes)
        timeoutSeconds = Self.plain(config.timeoutSeconds)
        offlineWarnAfterHours = Self.plain(config.offlineWarnAfterHours)
        maxDisappearancePct = Self.plain(config.maxDisappearancePct)
        maxDisappearanceCount = String(config.maxDisappearanceCount)
        windowBackDays = String(config.windowBackDays)
        windowForwardDays = String(config.windowForwardDays)
        pairs = config.pairs
    }

    /// `10.0` reads as a mistake in a box somebody is about to edit; `10` does
    /// not. Only the trailing `.0` goes — `0.2` stays `0.2`.
    static func plain(_ value: Double) -> String {
        value == value.rounded() && abs(value) < 1e15
            ? String(Int(value))
            : String(value)
    }

    /// The draft as a `Config`, or every reason it is not one.
    ///
    /// A blank number means the default rather than a refusal, which is the
    /// same tolerance `Config.init(from:)` extends to a file written by an
    /// older build: a field somebody cleared is a field they have no opinion
    /// about, and refusing to save over it teaches nothing.
    public func resolve() throws -> Config {
        var problems: [ConfigProblem] = []
        let defaults = Config(radicaleURL: "", pairs: [])

        func url(_ raw: String, _ field: ConfigField, required: Bool) -> String? {
            let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else {
                if required {
                    problems.append(ConfigProblem(
                        field: field,
                        message: "There is nowhere to read from until this is set — "
                            + "the principal, e.g. http://homebox:5232/calsync"))
                }
                return nil
            }
            // Checked here rather than at first use because both of these fail
            // silently otherwise: a mistyped Radicale URL reads as being
            // off-site, and a mistyped API URL reads as having no review badge.
            // Neither says anything, and the settings window is where somebody
            // would go looking.
            guard let parsed = URL(string: text), let scheme = parsed.scheme,
                  ["http", "https"].contains(scheme.lowercased()),
                  let host = parsed.host, !host.isEmpty
            else {
                problems.append(ConfigProblem(
                    field: field,
                    message: "\(text.debugDescription) is not an http:// or https:// "
                        + "address with a host in it"))
                return nil
            }
            return text
        }

        func integer(
            _ raw: String, _ field: ConfigField, _ range: ClosedRange<Int>,
            _ label: String, _ fallback: Int
        ) -> Int {
            let text = raw.trimmingCharacters(in: .whitespaces)
            guard !text.isEmpty else { return fallback }
            guard let value = Int(text) else {
                problems.append(ConfigProblem(
                    field: field,
                    message: "\(label) has to be a whole number, not \(text.debugDescription)"))
                return fallback
            }
            guard range.contains(value) else {
                problems.append(ConfigProblem(
                    field: field,
                    message: "\(label) has to be between \(range.lowerBound) and "
                        + "\(range.upperBound)."))
                return fallback
            }
            return value
        }

        func decimal(
            _ raw: String, _ field: ConfigField, _ range: ClosedRange<Double>,
            _ label: String, _ fallback: Double
        ) -> Double {
            let text = raw.trimmingCharacters(in: .whitespaces)
            guard !text.isEmpty else { return fallback }
            guard let value = Double(text) else {
                problems.append(ConfigProblem(
                    field: field,
                    message: "\(label) has to be a number, not \(text.debugDescription)"))
                return fallback
            }
            guard range.contains(value) else {
                problems.append(ConfigProblem(
                    field: field,
                    message: "\(label) has to be between \(Self.plain(range.lowerBound)) "
                        + "and \(Self.plain(range.upperBound))."))
                return fallback
            }
            return value
        }

        let radicale = url(radicaleURL, .radicaleURL, required: true)
        let api = url(apiURL, .apiURL, required: false)

        // The two halves of the review badge are useless apart: an address with
        // no token gets a 401 every interval, and a token with no address is
        // never sent anywhere. Either both or neither, said now rather than
        // discovered as a badge that never appears.
        let token = apiToken.trimmingCharacters(in: .whitespacesAndNewlines)
        if api != nil, token.isEmpty {
            problems.append(ConfigProblem(
                field: .apiToken,
                message: "The API needs a bearer token — it is the only service in "
                    + "the stack with a credential in front of it."))
        }
        if api == nil, !token.isEmpty,
           apiURL.trimmingCharacters(in: .whitespaces).isEmpty {
            problems.append(ConfigProblem(
                field: .apiURL,
                message: "There is a token here but no API address to send it to."))
        }

        // Percent first, and `20` means twenty percent — the same allowance the
        // console makes, because a box labelled with a % is one somebody types
        // 20 into.
        var pct = maxDisappearancePct.trimmingCharacters(in: .whitespaces)
        if let raw = Double(pct), raw > 1 { pct = Self.plain(raw / 100) }

        let resolved = Config(
            radicaleURL: radicale ?? "",
            username: username.isEmpty ? nil : username,
            password: password.isEmpty ? nil : password,
            pairs: Self.normalised(pairs),
            windowBackDays: integer(
                windowBackDays, .windowBackDays, ConfigLimits.windowBackDays,
                "Window back days", defaults.windowBackDays),
            windowForwardDays: integer(
                windowForwardDays, .windowForwardDays, ConfigLimits.windowForwardDays,
                "Window forward days", defaults.windowForwardDays),
            maxDisappearancePct: decimal(
                pct, .maxDisappearancePct, ConfigLimits.disappearancePct,
                "The disappearance percentage", defaults.maxDisappearancePct),
            maxDisappearanceCount: integer(
                maxDisappearanceCount, .maxDisappearanceCount,
                ConfigLimits.disappearanceCount,
                "The disappearance count", defaults.maxDisappearanceCount),
            timeoutSeconds: decimal(
                timeoutSeconds, .timeoutSeconds, ConfigLimits.timeoutSeconds,
                "The timeout", defaults.timeoutSeconds),
            offlineWarnAfterHours: decimal(
                offlineWarnAfterHours, .offlineWarnAfterHours,
                ConfigLimits.offlineWarnAfterHours,
                "The offline warning delay", defaults.offlineWarnAfterHours),
            syncIntervalMinutes: integer(
                syncIntervalMinutes, .syncIntervalMinutes,
                ConfigLimits.syncIntervalMinutes,
                "The sync interval", defaults.syncIntervalMinutes),
            apiURL: api,
            apiToken: api == nil ? nil : token)

        problems.append(contentsOf: Self.pairProblems(resolved.pairs))
        guard problems.isEmpty else { throw ConfigRefused(problems) }
        return resolved
    }

    /// Rows as the window collected them, made into pairs.
    ///
    /// Trimmed, wholly blank rows dropped — an editor with an "add" button ends
    /// every session with an empty row somebody stopped filling in — and exact
    /// repeats folded together, since the same instruction twice is one
    /// instruction and the second read costs a round trip to say nothing. Order
    /// is preserved, because the file is read by people.
    public static func normalised(_ pairs: [Pair]) -> [Pair] {
        var seen = Set<Pair>()
        return pairs
            .map {
                Pair(collection: $0.collection.trimmingCharacters(in: .whitespaces),
                     calendar: $0.calendar.trimmingCharacters(in: .whitespaces))
            }
            .filter { !($0.collection.isEmpty && $0.calendar.isEmpty) }
            .filter { seen.insert($0).inserted }
    }

    /// Collections that are holding pens, never destinations.
    ///
    /// `onboarding` and `enrichment` hold events calsync could not place, and
    /// putting one of those in front of the whole family is precisely what they
    /// exist to prevent. They stay visible in `/review` and to a calendar
    /// client pointed at Radicale, which is where somebody who wants to see
    /// them should look. A free-text collection box is the one place a person
    /// could type one, so it is the place to refuse it.
    public static let holdingPens: Set<String> = ["onboarding", "enrichment"]

    static func pairProblems(_ pairs: [Pair]) -> [ConfigProblem] {
        var problems: [ConfigProblem] = []
        func refuse(_ message: String) {
            problems.append(ConfigProblem(field: .pairs, message: message))
        }

        if pairs.isEmpty {
            refuse("Nothing is being mirrored — add a collection and the calendar "
                + "it should be written to.")
        }
        for pair in pairs {
            if pair.collection.isEmpty {
                refuse("A calendar is named (\(pair.calendar.debugDescription)) with "
                    + "no collection to fill it from.")
            } else if pair.calendar.isEmpty {
                refuse("Collection \(pair.collection.debugDescription) has no calendar "
                    + "to write to.")
            }
            if holdingPens.contains(pair.collection.lowercased()) {
                refuse("\(pair.collection.debugDescription) is a holding pen, not a "
                    + "calendar to mirror — it holds events calsync could not place, "
                    + "and they are meant to wait in /review rather than land on "
                    + "everybody's phone.")
            }
        }

        // Two collections into one calendar is the failure this pass exists
        // for. `Reconcile.plan` is given one collection's events and the
        // *whole* destination calendar, so every marked event the other
        // collection wrote looks like something that vanished upstream — each
        // run would delete the other's work and recreate its own. The guard
        // would catch the big version of that and wave the small one through,
        // which is exactly the shape of failure this tool is built around.
        var byCalendar: [String: [String]] = [:]
        for pair in pairs where !pair.calendar.isEmpty {
            byCalendar[pair.calendar, default: []].append(pair.collection)
        }
        for (calendar, collections) in byCalendar.sorted(by: { $0.key < $1.key })
        where collections.count > 1 {
            refuse("\(collections.sorted().joined(separator: " and ")) are both aimed "
                + "at \(calendar.debugDescription). Each run is planned against the "
                + "whole calendar, so the two would take turns deleting each other's "
                + "events. Give them separate calendars.")
        }
        return problems
    }
}
