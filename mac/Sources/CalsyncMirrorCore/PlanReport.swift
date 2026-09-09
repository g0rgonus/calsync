import Foundation

/// A plan, as lines somebody reads.
///
/// In Core rather than beside the writer because it is pure — a `MirrorPlan`
/// in, strings out — and because presentation is where this tool's bugs have
/// actually been. A menu item that never stopped saying "Syncing…" and a
/// duplicate report nobody could read both shipped past a green test suite by
/// living somewhere nothing could assert on them.
public enum PlanReport {

    public static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEE d MMM HH:mm"
        return formatter
    }()

    /// Why this event is being rewritten, as `[start +3h, end +3h]`.
    ///
    /// The log used to say only *that* something updated, which is the fact
    /// nobody can act on. When travelling rewrote 64 of 67 events, working out
    /// whether that was the feed moving or this tool disagreeing with itself
    /// needed a `PROPFIND` against Radicale for `getlastmodified` and a
    /// screenshot of the event on two machines. Both answers were already in
    /// this process; it simply did not say them.
    ///
    /// A time carries its shift, because the *amount* is the diagnosis. Sixty
    /// events all moving by exactly one offset is a timezone; one moving by
    /// twenty minutes is a coach. Nothing else needs a value — a title or a
    /// location that differs is read off the event itself.
    static func reason(for update: PlannedUpdate) -> String {
        let changed = Reconcile.changedFields(update.existing, update.fields)
        guard !changed.isEmpty else { return "" }
        let parts = changed.map { field -> String in
            switch field {
            case .start:
                return "start " + shift(from: update.existing.start, to: update.fields.start)
            case .end:
                return "end " + shift(from: update.existing.end, to: update.fields.end)
            default:
                return field.label
            }
        }
        return "  [" + parts.joined(separator: ", ") + "]"
    }

    /// A signed, human-sized duration: `+3h`, `-45m`, `+1d2h`.
    static func shift(from: Date, to: Date) -> String {
        let delta = to.timeIntervalSince(from)
        let sign = delta < 0 ? "-" : "+"
        var seconds = Int(abs(delta).rounded())
        var out = ""
        for (unit, size) in [("d", 86400), ("h", 3600), ("m", 60)] {
            let count = seconds / size
            if count > 0 { out += "\(count)\(unit)"; seconds -= count * size }
        }
        // Sub-minute differences are real (the comparison tolerance is a
        // second) and must not render as a bare sign.
        if out.isEmpty { out = "\(seconds)s" }
        return sign + out
    }

    public static func describe(_ plan: MirrorPlan) -> [String] {
        var lines: [String] = []
        for item in plan.creates {
            lines.append("  + create  \(stamp.string(from: item.fields.start))  "
                + item.fields.title)
        }
        for item in plan.updates {
            lines.append("  ~ update  \(stamp.string(from: item.fields.start))  "
                + item.fields.title + reason(for: item))
        }
        for item in plan.deletes {
            lines.append("  - delete  \(stamp.string(from: item.start))  \(item.title)")
        }
        if plan.unchanged > 0 { lines.append("  = \(plan.unchanged) unchanged") }
        if let hold = plan.hold {
            lines.append("  HELD: \(hold.message)")
            lines.append("        Nothing was deleted. If this is a real cancellation, "
                + "re-run once Radicale is confirmed good.")
        }
        if plan.isEmpty && plan.hold == nil && plan.unchanged == 0 {
            lines.append("  nothing to do")
        }
        // Grouped by what is already on the calendar, not listed per event.
        //
        // Against a real calendar this was 113 separate warnings for 48 events,
        // and no human reads that — they scroll past it, which is worse than no
        // report. Grouped, the same information is a dozen lines and the answer
        // is immediate: "James ⚽️ Practice ×38" is the season somebody synced
        // by hand, and "Patrick 🏃‍♂️ Drylands ×34" is a different kid's
        // training that merely overlaps it.
        //
        // The alternative was scoring each pair, which is the adoption matcher
        // `PLAN.md` §6a says to cut. Grouping gets the report readable without
        // deciding anything, and a person tells these apart at a glance.
        if !plan.duplicates.isEmpty {
            var counts: [String: Int] = [:]
            for duplicate in plan.duplicates {
                counts[duplicate.existingTitle, default: 0] += 1
            }
            let affected = Set(plan.duplicates.map(\.desiredStart)).count
            lines.append(
                "  ! \(affected) of these land within an hour of something already "
                + "on this calendar that calsync did not create:")
            for (title, count) in counts.sorted(by: {
                $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value
            }) {
                lines.append(String(format: "      %3d × %@", count,
                                    title.trimmingCharacters(in: .whitespaces)))
            }
            lines.append("    Nothing was deleted. Remove the hand-made copies in "
                + "Calendar.app if they are the same event.")
        }
        return lines
    }
}
