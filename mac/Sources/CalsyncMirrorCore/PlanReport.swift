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

    public static func describe(_ plan: MirrorPlan) -> [String] {
        var lines: [String] = []
        for item in plan.creates {
            lines.append("  + create  \(stamp.string(from: item.fields.start))  "
                + item.fields.title)
        }
        for item in plan.updates {
            lines.append("  ~ update  \(stamp.string(from: item.fields.start))  "
                + item.fields.title)
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
