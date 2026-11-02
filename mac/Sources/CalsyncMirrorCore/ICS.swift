import Foundation

/// A VEVENT, parsed only as far as this tool needs it.
///
/// Deliberately not a general iCalendar implementation. calsync is the only
/// writer this ever reads, so the surface is `targets/ics_file.py:to_vevent`
/// and nothing else — which is a known, tested, and quite small subset. A
/// general parser here would be a large amount of code standing between two
/// programs that already agree on a format.
public struct ParsedEvent: Equatable {
    /// The iCalendar UID, which for calsync is the event's own stable uid.
    public var uid: String
    /// `X-CALSYNC-UID`. Normally identical to `uid`; kept apart because it is
    /// the property that *means* "calsync made this", and identity should not
    /// rest on an assumption that two fields happen to agree.
    public var calsyncUID: String?
    public var summary: String
    public var description: String?
    public var location: String?
    public var url: String?
    /// Start instant. For an all-day event this is midnight of `DTSTART`'s date
    /// in `timeZoneID` (or the system zone), which is what "the day" means.
    public var start: Date
    /// End instant. For an all-day event this is **exclusive** — RFC 5545 DATE
    /// semantics, carried through faithfully. EventKit wants the last day
    /// inclusive, and that conversion belongs to the writer, not here.
    public var end: Date
    public var isAllDay: Bool
    public var timeZoneID: String?
    public var cancelled: Bool
    /// Seconds relative to start; negative means before. calsync writes
    /// `TRIGGER:-PT90M` for a game.
    public var alarmOffsetSeconds: Double?
    public var sourceID: String?
    public var activityID: String?
    public var contentHash: String?

    public init(
        uid: String, calsyncUID: String? = nil, summary: String,
        description: String? = nil, location: String? = nil, url: String? = nil,
        start: Date, end: Date, isAllDay: Bool = false, timeZoneID: String? = nil,
        cancelled: Bool = false, alarmOffsetSeconds: Double? = nil,
        sourceID: String? = nil, activityID: String? = nil, contentHash: String? = nil
    ) {
        self.uid = uid
        self.calsyncUID = calsyncUID
        self.summary = summary
        self.description = description
        self.location = location
        self.url = url
        self.start = start
        self.end = end
        self.isAllDay = isAllDay
        self.timeZoneID = timeZoneID
        self.cancelled = cancelled
        self.alarmOffsetSeconds = alarmOffsetSeconds
        self.sourceID = sourceID
        self.activityID = activityID
        self.contentHash = contentHash
    }
}

public enum ICSError: Error, CustomStringConvertible {
    case notCalendar(String)
    case truncated(String)

    public var description: String {
        switch self {
        case .notCalendar(let detail): return detail
        case .truncated(let detail): return detail
        }
    }
}

public enum ICS {

    // MARK: - Public entry point

    /// Parse every VEVENT in a calendar document.
    ///
    /// Throws on a body that is not a calendar at all. It deliberately does
    /// **not** throw on a calendar with zero events: an empty collection is a
    /// legitimate state (a season that has not started), and the disappearance
    /// guard — not the parser — is what decides whether zero is suspicious.
    /// The distinction matters because those two are the same shape, which is
    /// the trap `sync.py` orders itself around.
    public static func parseCalendar(_ text: String) throws -> [ParsedEvent] {
        let lines = unfold(text)
        guard lines.contains(where: { $0.uppercased().hasPrefix("BEGIN:VCALENDAR") }) else {
            let head = text.prefix(120).replacingOccurrences(of: "\n", with: " ")
            throw ICSError.notCalendar(
                "response is not an iCalendar document (no BEGIN:VCALENDAR); got: \(head)")
        }
        // A body that starts as a calendar and stops partway is the dangerous
        // one: it parses, and the events lost off the end look exactly like
        // deletions. The disappearance guard catches a big truncation and would
        // wave through a small one, so completeness is checked here instead —
        // where it is a fact about the document rather than a judgement about
        // the season.
        guard lines.contains(where: { $0.uppercased().hasPrefix("END:VCALENDAR") }) else {
            throw ICSError.truncated(
                "the calendar ended without END:VCALENDAR — the response was cut short, "
                + "and the events missing from the end of it are not cancellations")
        }
        return blocks(in: lines, named: "VEVENT").compactMap { parseEvent($0) }
    }

    // MARK: - Unfolding

    /// RFC 5545 line unfolding: a line beginning with a space or tab continues
    /// the previous one. Feeds fold at 75 octets, so a long DESCRIPTION arrives
    /// in pieces and any parser that skips this loses the tail of every body.
    static func unfold(_ text: String) -> [String] {
        let normalized = text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        var lines: [String] = []
        for raw in normalized.split(separator: "\n", omittingEmptySubsequences: false) {
            if let first = raw.first, first == " " || first == "\t", !lines.isEmpty {
                lines[lines.count - 1] += raw.dropFirst()
            } else {
                lines.append(String(raw))
            }
        }
        return lines
    }

    // MARK: - Component blocks

    /// Line groups between `BEGIN:<name>` and its matching `END:<name>`,
    /// nesting-aware so a VALARM inside a VEVENT does not end the VEVENT.
    static func blocks(in lines: [String], named name: String) -> [[String]] {
        let begin = "BEGIN:\(name)"
        let end = "END:\(name)"
        var out: [[String]] = []
        var current: [String]? = nil
        var depth = 0
        for line in lines {
            let upper = line.uppercased().trimmingCharacters(in: .whitespaces)
            if upper == begin {
                if current == nil { current = []; depth = 1 } else { depth += 1; current?.append(line) }
            } else if upper == end, current != nil {
                depth -= 1
                if depth == 0 { out.append(current!); current = nil } else { current?.append(line) }
            } else if current != nil {
                current?.append(line)
            }
        }
        return out
    }

    // MARK: - Content lines

    struct ContentLine {
        let name: String
        let params: [String: String]
        let value: String
    }

    /// `NAME;PARAM=VALUE:the value`.
    ///
    /// The split is on the first colon **outside a quoted parameter**. A plain
    /// `firstIndex(of: ":")` truncates every `URL:https://…` at the scheme,
    /// which is exactly the property calsync uses for a Player360 deep link.
    static func parseLine(_ line: String) -> ContentLine? {
        var inQuotes = false
        var colon: String.Index? = nil
        var i = line.startIndex
        while i < line.endIndex {
            let c = line[i]
            if c == "\"" {
                inQuotes.toggle()
            } else if c == ":" && !inQuotes {
                colon = i
                break
            }
            i = line.index(after: i)
        }
        guard let ci = colon else { return nil }
        let head = String(line[line.startIndex..<ci])
        let value = String(line[line.index(after: ci)...])

        let parts = splitUnquoted(head, on: ";")
        guard let rawName = parts.first, !rawName.isEmpty else { return nil }

        var params: [String: String] = [:]
        for part in parts.dropFirst() {
            let kv = splitUnquoted(part, on: "=")
            guard kv.count >= 2 else { continue }
            let key = kv[0].uppercased()
            let raw = kv.dropFirst().joined(separator: "=")
            params[key] = raw.trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        }
        return ContentLine(name: rawName.uppercased(), params: params, value: value)
    }

    static func splitUnquoted(_ s: String, on sep: Character) -> [String] {
        var out: [String] = []
        var buf = ""
        var inQuotes = false
        for c in s {
            if c == "\"" { inQuotes.toggle(); buf.append(c) }
            else if c == sep && !inQuotes { out.append(buf); buf = "" }
            else { buf.append(c) }
        }
        out.append(buf)
        return out
    }

    /// RFC 5545 TEXT unescaping. `\n` is a real newline in a DESCRIPTION, and
    /// calsync's bodies are multi-line by construction.
    static func unescapeText(_ s: String) -> String {
        var out = ""
        var escaped = false
        for c in s {
            if escaped {
                switch c {
                case "n", "N": out.append("\n")
                case "\\": out.append("\\")
                case ",": out.append(",")
                case ";": out.append(";")
                default: out.append(c)
                }
                escaped = false
            } else if c == "\\" {
                escaped = true
            } else {
                out.append(c)
            }
        }
        if escaped { out.append("\\") }
        return out
    }

    // MARK: - Event

    static func parseEvent(_ lines: [String]) -> ParsedEvent? {
        // The alarm sub-block is extracted first, then excluded, so a VALARM's
        // own DESCRIPTION cannot be mistaken for the event's.
        let alarmBlocks = blocks(in: lines, named: "VALARM")
        var alarmOffset: Double? = nil
        for block in alarmBlocks {
            for line in block {
                guard let cl = parseLine(line), cl.name == "TRIGGER" else { continue }
                if let seconds = parseDuration(cl.value) { alarmOffset = seconds; break }
            }
            if alarmOffset != nil { break }
        }

        var body: [String] = []
        var depth = 0
        for line in lines {
            let upper = line.uppercased().trimmingCharacters(in: .whitespaces)
            if upper.hasPrefix("BEGIN:") { depth += 1; continue }
            if upper.hasPrefix("END:") { depth -= 1; continue }
            if depth == 0 { body.append(line) }
        }

        var uid: String? = nil
        var calsyncUID: String? = nil
        var summary = ""
        var description: String? = nil
        var location: String? = nil
        var url: String? = nil
        var startInfo: (Date, Bool, String?)? = nil
        var endInfo: (Date, Bool, String?)? = nil
        var cancelled = false
        var sourceID: String? = nil
        var activityID: String? = nil
        var contentHash: String? = nil

        for line in body {
            guard let cl = parseLine(line) else { continue }
            switch cl.name {
            case "UID": uid = unescapeText(cl.value)
            case "SUMMARY": summary = unescapeText(cl.value)
            case "DESCRIPTION": description = unescapeText(cl.value)
            case "LOCATION": location = unescapeText(cl.value)
            case "URL": url = cl.value
            case "STATUS": cancelled = cl.value.uppercased() == "CANCELLED"
            case "DTSTART": startInfo = parseDateValue(cl.value, params: cl.params)
            case "DTEND": endInfo = parseDateValue(cl.value, params: cl.params)
            case "X-CALSYNC-UID": calsyncUID = unescapeText(cl.value)
            case "X-CALSYNC-SOURCE": sourceID = unescapeText(cl.value)
            case "X-CALSYNC-ACTIVITY": activityID = unescapeText(cl.value)
            case "X-CALSYNC-HASH": contentHash = unescapeText(cl.value)
            default: break
            }
        }

        guard let resolvedUID = uid, let (start, allDay, tzid) = startInfo else { return nil }
        // A VEVENT may omit DTEND, in which case it is a point in time.
        let end = endInfo?.0 ?? start

        return ParsedEvent(
            uid: resolvedUID,
            calsyncUID: calsyncUID,
            summary: summary,
            description: description,
            location: location,
            url: url,
            start: start,
            end: end,
            isAllDay: allDay,
            timeZoneID: tzid,
            cancelled: cancelled,
            alarmOffsetSeconds: alarmOffset,
            sourceID: sourceID,
            activityID: activityID,
            contentHash: contentHash
        )
    }

    // MARK: - Dates

    /// Returns the instant, whether it was a DATE, and the zone it named.
    ///
    /// Three forms reach us and they are not interchangeable: `20260315`
    /// (a date, no time — a tournament day), `20260315T190000Z` (UTC), and
    /// `TZID=America/New_York:20260315T140000`. calsync writes the TZID form
    /// without a matching VTIMEZONE, which is legal-adjacent and works because
    /// the identifiers are Olson names that `TimeZone(identifier:)` resolves.
    static func parseDateValue(
        _ value: String, params: [String: String]
    ) -> (Date, Bool, String?)? {
        let raw = value.trimmingCharacters(in: .whitespaces)
        let isDate = params["VALUE"]?.uppercased() == "DATE"
            || (raw.count == 8 && !raw.contains("T"))

        var calendar = Calendar(identifier: .gregorian)
        var zoneID = params["TZID"]

        if raw.hasSuffix("Z") {
            calendar.timeZone = TimeZone(identifier: "UTC")!
            zoneID = nil
        } else if let id = zoneID, let tz = TimeZone(identifier: id) {
            calendar.timeZone = tz
        } else {
            // A floating time, or a TZID this machine cannot resolve. The
            // system zone is the only sensible reading, and it is what a
            // calendar client does with a floating value too.
            calendar.timeZone = TimeZone.current
            if zoneID != nil { zoneID = nil }
        }

        let digits = raw.hasSuffix("Z") ? String(raw.dropLast()) : raw
        func number(_ from: Int, _ length: Int) -> Int? {
            guard digits.count >= from + length else { return nil }
            let s = digits.index(digits.startIndex, offsetBy: from)
            let e = digits.index(s, offsetBy: length)
            return Int(digits[s..<e])
        }
        guard let year = number(0, 4), let month = number(4, 2), let day = number(6, 2) else {
            return nil
        }

        var components = DateComponents()
        components.year = year
        components.month = month
        components.day = day
        if isDate {
            components.hour = 0
            components.minute = 0
            components.second = 0
        } else {
            components.hour = number(9, 2) ?? 0
            components.minute = number(11, 2) ?? 0
            components.second = number(13, 2) ?? 0
        }
        guard let date = calendar.date(from: components) else { return nil }
        return (date, isDate, isDate ? nil : zoneID)
    }

    /// ISO 8601 duration as iCalendar uses it: `-PT90M`, `PT1H30M`, `-P1D`.
    static func parseDuration(_ value: String) -> Double? {
        var s = value.trimmingCharacters(in: .whitespaces).uppercased()
        var sign = 1.0
        if s.hasPrefix("-") { sign = -1; s.removeFirst() }
        else if s.hasPrefix("+") { s.removeFirst() }
        guard s.hasPrefix("P") else { return nil }
        s.removeFirst()

        var total = 0.0
        var number = ""
        var inTime = false
        var sawUnit = false
        for c in s {
            if c == "T" { inTime = true; number = ""; continue }
            if c.isNumber { number.append(c); continue }
            guard let n = Double(number) else { return nil }
            switch c {
            case "W": total += n * 604800
            case "D": total += n * 86400
            case "H": total += n * 3600
            case "M": total += inTime ? n * 60 : n * 2592000
            case "S": total += n
            default: return nil
            }
            sawUnit = true
            number = ""
        }
        guard sawUnit else { return nil }
        return sign * total
    }
}
