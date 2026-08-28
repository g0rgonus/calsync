import EventKit
import Foundation
import CalsyncMirrorCore
import CalsyncMirrorEventKit

/// Exit codes match calsync's, because a scheduler should read them the same
/// way: 0 ok, 1 error, 3 a guard held. 4 is this tool's own — it could not see
/// Radicale — and it is deliberately not 1: a laptop away from home is not a
/// broken deployment, and a job that reports one as the other is a job whose
/// log nobody reads.
enum Exit: Int32 {
    case ok = 0
    case error = 1
    case held = 3
    case unreachable = 4
}

@main
struct CalsyncMirror {

    static let usage = """
        calsync-mirror — mirror calsync's Radicale collections into Apple Calendar

        USAGE
          calsync-mirror [--dry-run] [--config <path>]
          calsync-mirror --check [--config <path>]
          calsync-mirror --init [--config <path>]

        OPTIONS
          --check      Can Radicale be reached and parsed? Touches no calendar
                       and asks for no permission. Run this first of all.
          --dry-run    Show the plan and change nothing. Run this second.
          --config     Config file (default: ~/.config/calsync-mirror/config.json)
          --init       Write a sample config and exit.
          --version    Print the version and exit.
          --help       This.

        Events this tool created carry a "\(Marker.prefix)" line in their
        notes. It will never modify or delete an event without one.

        OFF-SITE
          Every collection is read before any calendar is opened, so a run that
          cannot see Radicale changes nothing, asks for no calendar permission,
          and prints one line. It says how long it has been since a read last
          worked, and gets louder once that stops looking like a trip away.

        EXIT CODES
          0  ok                    3  a guard held; deletions withheld
          1  error                 4  Radicale unreachable; nothing was done
        """

    static func main() async {
        var dryRun = false
        var doInit = false
        var checkOnly = false
        var configPath = Config.defaultPath

        var args = Array(CommandLine.arguments.dropFirst())
        while !args.isEmpty {
            let arg = args.removeFirst()
            switch arg {
            case "--dry-run", "-n": dryRun = true
            case "--init": doInit = true
            case "--check": checkOnly = true
            case "--version":
                // Bare, so `install.sh` can read it straight into Info.plist
                // without a parse step to get wrong.
                print(Build.version)
                exit(Exit.ok.rawValue)
            case "--help", "-h":
                print(usage)
                exit(Exit.ok.rawValue)
            case "--config":
                guard !args.isEmpty else { fail("--config needs a path") }
                configPath = URL(fileURLWithPath: (args.removeFirst() as NSString)
                    .expandingTildeInPath)
            default:
                fail("unknown argument \(arg.debugDescription)\n\n\(usage)")
            }
        }

        if doInit {
            do {
                guard !FileManager.default.fileExists(atPath: configPath.path) else {
                    fail("\(configPath.path) already exists; not overwriting it")
                }
                try Config.sample.write(to: configPath)
                print("Wrote \(configPath.path)")
                print("Edit it, then run: calsync-mirror --check")
                exit(Exit.ok.rawValue)
            } catch {
                fail("could not write \(configPath.path): \(error)")
            }
        }

        let config: Config
        do {
            config = try Config.load(from: configPath)
        } catch {
            fail("""
                could not read \(configPath.path): \(error)

                Run `calsync-mirror --init` to write a starting point.
                """)
        }
        guard !config.pairs.isEmpty else { fail("no pairs configured in \(configPath.path)") }

        let now = Date()

        // Phase 1 — read everything before opening a calendar.
        //
        // The ordering is the same safety `sync.py` is built around: a failed
        // read must never reach the part that decides what to delete. It also
        // means an off-site run never touches EventKit, so it cannot prompt for
        // calendar access on a network where it could do nothing anyway.
        if checkOnly {
            exit(reportCheck(await readAll(config), config: config, now: now).rawValue)
        }

        let pause = Pause.load()
        if pause.isActive(at: now) {
            let formatter = DateFormatter()
            formatter.dateFormat = "EEE d MMM HH:mm"
            print(pause.describe(at: now, formatter: formatter)
                + " — resume from the menu bar, or delete "
                + Pause.defaultPath.path)
            exit(Exit.ok.rawValue)
        }

        let runner = SyncRunner(config: config, store: CalendarStore())

        if dryRun { print("DRY RUN — nothing will be written\n") }
        let summary = await runner.run(now: now, dryRun: dryRun)
        for line in summary.lines { print(line) }

        if summary.offline { exit(Exit.unreachable.rawValue) }
        if !summary.errors.isEmpty { exit(Exit.error.rawValue) }
        if !summary.holds.isEmpty { exit(Exit.held.rawValue) }
        exit(Exit.ok.rawValue)
    }

    // MARK: - Reading

    static func readAll(_ config: Config) async -> [CollectionRead] {
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

    /// Read every configured collection and say what came back.
    ///
    /// Deliberately reachable without calendar access: "can this machine read
    /// Radicale" and "may this tool touch your calendars" are separate
    /// questions, and answering the first should not require granting the
    /// second.
    ///
    /// It records a *success* and never a failure. The asymmetry is the honest
    /// one: a read that demonstrably worked makes "unreachable for five days"
    /// false, and leaving it standing would have the tool warn about a state it
    /// had just disproved. Failures stay out because a manual probe from a
    /// coffee shop should not inflate the scheduled job's account of things.
    static func reportCheck(_ reads: [CollectionRead], config: Config, now: Date) -> Exit {
        if reads.allSatisfy({ $0.isUnreachable }) {
            print("  offline  none of \(reads.count) collection(s) could be reached")
            if let first = reads.first?.error { print("           \(first)") }
            let health = Health.load()
            if let last = health.lastReachedAt {
                print("           last successful read: "
                    + "\(Health.describe(now.timeIntervalSince(last))) ago")
            }
            return .unreachable
        }

        // Something answered, so whatever the health file said about a long
        // silence is now out of date.
        var health = Health.load()
        health.recordReached(at: now)
        health.save()

        var status = Exit.ok
        for read in reads {
            if let error = read.error {
                print("  FAIL  \(read.pair.collection): \(error)")
                status = .error
                continue
            }
            let events = read.events ?? []
            let upcoming = events.filter { $0.start >= now }.count
            print("  ok    \(read.pair.collection): \(events.count) events "
                + "(\(upcoming) upcoming) → would write to \(read.pair.calendar.debugDescription)")
            if let first = events.min(by: { $0.start < $1.start }) {
                print("        earliest: \(stamp.string(from: first.start))  \(first.summary)")
            }
            if events.contains(where: { $0.calsyncUID == nil }) {
                print("        note: some events carry no X-CALSYNC-UID — "
                    + "is this collection written by calsync?")
            }
        }
        return status
    }

    static func emit(_ report: OfflineReport) {
        switch report {
        case .quiet(let message):
            print(message)
        case .warn(let message):
            FileHandle.standardError.write(Data(("WARNING: " + message + "\n").utf8))
        }
    }

    // MARK: - Output

    static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEE d MMM HH:mm"
        return formatter
    }()

    static func fail(_ message: String) -> Never {
        FileHandle.standardError.write(Data(("ERROR: " + message + "\n").utf8))
        exit(Exit.error.rawValue)
    }
}
