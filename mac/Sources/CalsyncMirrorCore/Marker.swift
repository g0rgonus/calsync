import Foundation

/// The line that says an event is calsync's, written into the notes.
///
/// **This is load-bearing, not decoration.** EventKit gives no way to set an
/// event's UID — `calendarItemExternalIdentifier` is read-only and iCloud
/// assigns it on create — so calsync's stable uid cannot travel in the field
/// built for it, and the `X-CALSYNC-UID` property on the VEVENT does not
/// survive into EKEvent either. A writable free-text field is the only channel
/// left, which makes the notes line the sole durable record of which events
/// this tool owns.
///
/// That it is also human-readable is the point: the family sees at a glance
/// which entries are managed and which somebody typed.
///
/// Identity living in the events themselves — rather than only in the state
/// file — is what makes a lost state file a re-index instead of a duplicate of
/// every event on the calendar.
public enum Marker {
    public static let prefix = "Managed by calsync"
    static let uidTag = "uid:"

    public static func line(uid: String) -> String {
        "\(prefix) — \(uidTag)\(uid)"
    }

    /// The calsync uid recorded in a notes field, if it carries one.
    public static func uid(inNotes notes: String?) -> String? {
        guard let notes else { return nil }
        for raw in notes.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard line.hasPrefix(prefix), let r = line.range(of: uidTag) else { continue }
            let uid = String(line[r.upperBound...]).trimmingCharacters(in: .whitespaces)
            if !uid.isEmpty { return uid }
        }
        return nil
    }

    public static func isManaged(notes: String?) -> Bool {
        uid(inNotes: notes) != nil
    }

    /// The notes to write: calsync's own body, then the marker.
    public static func notes(body: String?, uid: String) -> String {
        let trimmed = (body ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? line(uid: uid) : "\(trimmed)\n\n\(line(uid: uid))"
    }
}
