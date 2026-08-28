import EventKit
import Foundation
import CalsyncMirrorCore

/// What one pass over every pair did.
public struct RunSummary {
    public var created = 0
    public var updated = 0
    public var deleted = 0
    public var unchanged = 0
    public var duplicates: [DuplicateWarning] = []
    public var holds: [String] = []
    public var errors: [String] = []
    /// Nothing could be reached. Distinct from an error, and the reason the
    /// menu can stay quiet on a trip away.
    public var offline = false
    public var paused = false
    /// Per-pair lines, for the CLI to print.
    public var lines: [String] = []

    public var changed: Int { created + updated + deleted }

    public var outcome: SyncOutcome {
        let now = Date()
        if offline { return .offline(at: now) }
        if let hold = holds.first { return .held(hold, at: now) }
        if let error = errors.first { return .failed(error, at: now) }
        return .ok(created: created, updated: updated, deleted: deleted, at: now)
    }
}

/// One pass of the mirror, shared by the CLI and the menu bar app.
///
/// Both front ends run *this*, rather than each orchestrating the steps
/// themselves. Two implementations of "what does a sync do" would be two things
/// to keep in step, and the one that drifted would be the one that deletes from
/// a calendar four people read.
public final class SyncRunner {
    let config: Config
    let store: CalendarStore

    public init(config: Config, store: CalendarStore) {
        self.config = config
        self.store = store
    }

    /// Read everything, then write. The ordering is the safety, the same as
    /// `sync.py`: a failed read must never reach the part that decides what to
    /// delete, and an unreachable run must not open a calendar at all.
    public func run(now: Date = Date(), dryRun: Bool = false) async -> RunSummary {
        var summary = RunSummary()

        let pause = Pause.load()
        if pause.isActive(at: now) {
            summary.paused = true
            return summary
        }

        let reads = await readAll()
        var health = Health.load()
        if !reads.isEmpty, reads.allSatisfy({ $0.isUnreachable }) {
            health.recordUnreachable(at: now)
            health.save()
            summary.offline = true
            summary.lines.append(
                health.report(now: now, warnAfterHours: config.offlineWarnAfterHours)
                    .message)
            return summary
        }
        health.recordReached(at: now)
        health.save()

        // Only now. Radicale answered, so there is something to write and a
        // prompt is warranted; asking before the read would put a permission
        // dialog in front of somebody on a network where nothing could be done.
        do {
            try await store.requestAccess()
        } catch {
            summary.errors.append("\(error)")
            return summary
        }

        let calendar = Calendar.current
        let windowStart = calendar.date(
            byAdding: .day, value: -config.windowBackDays, to: now) ?? now
        let windowEnd = calendar.date(
            byAdding: .day, value: config.windowForwardDays, to: now) ?? now

        for read in reads {
            summary.lines.append("[\(read.pair.collection) → \(read.pair.calendar)]")
            if let error = read.error {
                summary.errors.append("\(read.pair.collection): \(error)")
                summary.lines.append("  ERROR \(error)")
                continue
            }

            do {
                let target = try store.calendar(titled: read.pair.calendar)
                let existing = store.existing(in: target, from: windowStart, to: windowEnd)
                let windowed = (read.events ?? []).filter {
                    $0.start >= windowStart && $0.start <= windowEnd
                }
                let plan = Reconcile.plan(
                    desired: windowed, existing: existing, now: now,
                    guardPolicy: config.policy, calendar: calendar)

                summary.lines.append(
                    "  read \(windowed.count) from Radicale, "
                    + "\(existing.filter { $0.calsyncUID != nil }.count) already mirrored")
                summary.lines.append(contentsOf: PlanReport.describe(plan))
                summary.unchanged += plan.unchanged
                summary.duplicates.append(contentsOf: plan.duplicates)
                if let hold = plan.hold { summary.holds.append(hold.message) }

                if dryRun {
                    summary.created += plan.creates.count
                    summary.updated += plan.updates.count
                    summary.deleted += plan.deletes.count
                    continue
                }

                for item in plan.creates {
                    do { try store.create(item, in: target); summary.created += 1 }
                    catch { summary.errors.append("creating \(item.fields.title): \(error)") }
                }
                for item in plan.updates {
                    do { try store.update(item); summary.updated += 1 }
                    catch { summary.errors.append("updating \(item.fields.title): \(error)") }
                }
                for item in plan.deletes {
                    do { try store.delete(item); summary.deleted += 1 }
                    catch { summary.errors.append("deleting \(item.title): \(error)") }
                }
            } catch {
                summary.errors.append("\(read.pair.calendar): \(error)")
                summary.lines.append("  ERROR \(error)")
            }
        }
        return summary
    }

    public func readAll() async -> [CollectionRead] {
        let client: RadicaleClient
        do {
            client = try RadicaleClient(config: config)
        } catch {
            return config.pairs.map { CollectionRead(pair: $0, events: nil, error: error) }
        }
        return await Reader.readAll(pairs: config.pairs) {
            try await client.fetch(collection: $0)
        }
    }

}
