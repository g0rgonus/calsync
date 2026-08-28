import AppKit
import Foundation
import OSLog
import UserNotifications
import CalsyncMirrorCore
import CalsyncMirrorEventKit

/// The menu bar app.
///
/// A second front end over `SyncRunner`, not a second implementation. Every
/// decision — what to write, what to delete, when to withhold — happens in
/// `CalsyncMirrorCore` exactly as it does for the CLI, and is tested there
/// without a Mac. What lives here is a status item, a timer, and a menu.
///
/// It deliberately cannot resolve anything. Status, pause, sync now and links
/// out are all about *this machine*; answering a question or approving an
/// answer happens in the console, by a person, because that is where the review
/// gate is (docs/API.md).
/// Where a background app says what it did.
///
/// Both, deliberately. `print` lands in the launchd log file, which is where
/// somebody who read `install.sh` will look. `Logger` goes to the unified log,
/// which is the only one that survives being launched some other way — by
/// `open`, or from Login Items — and is retrievable after the fact with
/// `log show --predicate 'subsystem == "com.goergen.calsync-mirror"'`.
/// An app that runs at login and keeps no record is one you cannot debug from
/// the outside, and this one runs unattended for weeks at a time.
enum Log {
    static let logger = Logger(subsystem: "com.goergen.calsync-mirror", category: "sync")

    static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()

    static func say(_ message: String) {
        print("[\(stamp.string(from: Date()))] \(message)")
        fflush(stdout)
        logger.log("\(message, privacy: .public)")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {

    var statusItem: NSStatusItem!
    var config: Config?
    var timer: Timer?
    var outcome: SyncOutcome = .never
    var review: ReviewCounts?
    var syncing = false
    /// So a held run or a long outage is announced once, not every interval.
    var announced: String?

    let formatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.menu = NSMenu()
        statusItem.menu?.delegate = self

        do {
            config = try Config.load(from: Config.defaultPath)
        } catch {
            let message = "no usable config at \(Config.defaultPath.path) — run "
                + "`calsync-mirror --init`"
            Log.say("ERROR \(message): \(error)")
            outcome = .failed(message, at: Date())
        }

        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert]) { _, _ in }

        Log.say("Calsync Mirror \(Build.version) started; "
            + "syncing every \(config?.syncIntervalMinutes ?? 15)m")
        refresh()
        scheduleTimer()
        Task { await syncNow() }
    }

    func scheduleTimer() {
        timer?.invalidate()
        let minutes = Double(config?.syncIntervalMinutes ?? 15)
        timer = Timer.scheduledTimer(
            withTimeInterval: max(60, minutes * 60), repeats: true
        ) { [weak self] _ in
            Task { await self?.syncNow() }
        }
    }

    // MARK: - Syncing

    @MainActor
    func syncNow() async {
        guard let config, !syncing else { return }
        syncing = true
        refresh()
        // Cleared explicitly rather than by `defer`, which runs *after* the
        // last `refresh()` below and so left the icon spinning until something
        // else happened to redraw it.

        let runner = SyncRunner(config: config, store: CalendarStore())
        let summary = await runner.run()

        if summary.paused {
            Log.say("paused; nothing done")
        } else {
            // `lines` already carries the errors in place, next to the pair
            // they belong to. Logging `errors` as well printed each one twice.
            for line in summary.lines { Log.say(line) }
            Log.say("run: \(summary.created) created, \(summary.updated) updated, "
                + "\(summary.deleted) deleted, \(summary.unchanged) unchanged"
                + (summary.offline ? " (offline)" : ""))
        }

        // A paused run did nothing and must not overwrite what the last real
        // run reported — otherwise pausing makes the menu look like it synced.
        if !summary.paused { outcome = summary.outcome }

        if let client = ReviewClient(config: config) {
            review = try? await client.fetch()
        }
        syncing = false
        refresh()
        announce()
    }

    /// One notification per distinct condition, not one per interval.
    ///
    /// Same shape as `enrichment.review`'s notify-once fingerprint, for the same
    /// reason: this runs every fifteen minutes, and a push each time is muted by
    /// lunchtime — which is worse than no push at all.
    func announce() {
        let status = currentStatus()
        guard status.needsAttention else { announced = nil; return }
        let fingerprint = status.title + (status.detail ?? "")
        guard fingerprint != announced else { return }
        announced = fingerprint

        let content = UNMutableNotificationContent()
        content.title = status.title
        content.body = status.detail ?? ""
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString,
                                  content: content, trigger: nil))
    }

    // MARK: - Presentation

    func currentStatus() -> MenuStatus {
        StatusPresenter.status(
            outcome: outcome,
            pause: Pause.load(),
            health: Health.load(),
            review: review,
            now: Date(),
            warnAfterHours: config?.offlineWarnAfterHours ?? 48,
            formatter: formatter)
    }

    func refresh() {
        let status = syncing
            ? MenuStatus(symbol: "arrow.triangle.2.circlepath", title: "Syncing…",
                         detail: nil, needsAttention: false)
            : currentStatus()

        let image = NSImage(systemSymbolName: status.symbol,
                            accessibilityDescription: status.title)
        image?.isTemplate = true
        statusItem.button?.image = image
        statusItem.button?.toolTip = status.title
    }

    func item(_ title: String, _ action: Selector?, key: String = "") -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
        entry.target = self
        return entry
    }

    func buildMenu() {
        guard let menu = statusItem.menu else { return }
        menu.removeAllItems()
        let status = currentStatus()

        let header = NSMenuItem(title: status.title, action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        for line in status.detailLines() {
            let detail = NSMenuItem(title: "  " + line, action: nil, keyEquivalent: "")
            detail.isEnabled = false
            menu.addItem(detail)
        }
        menu.addItem(.separator())

        menu.addItem(item(syncing ? "Syncing…" : "Sync Now", #selector(doSync), key: "s"))

        let pause = Pause.load()
        if pause.isActive(at: Date()) {
            menu.addItem(item("Resume", #selector(doResume)))
        } else {
            let submenu = NSMenu()
            for duration in Pause.Duration.allCases {
                let entry = NSMenuItem(
                    title: duration.label, action: #selector(doPause), keyEquivalent: "")
                entry.target = self
                entry.representedObject = duration.rawValue
                submenu.addItem(entry)
            }
            let parent = NSMenuItem(title: "Pause", action: nil, keyEquivalent: "")
            menu.addItem(parent)
            menu.setSubmenu(submenu, for: parent)
        }

        menu.addItem(.separator())
        if let review, !review.isQuiet {
            let entry = NSMenuItem(title: "Review: \(review.summary)",
                                   action: #selector(openConsole), keyEquivalent: "")
            entry.target = self
            menu.addItem(entry)
        }
        menu.addItem(item("Open Console…", #selector(openConsole)))
        menu.addItem(item("Open Config…", #selector(openConfig)))
        menu.addItem(.separator())
        let version = NSMenuItem(
            title: "Calsync Mirror \(Build.version)", action: nil,
            keyEquivalent: "")
        version.isEnabled = false
        menu.addItem(version)
        menu.addItem(item("Quit", #selector(quit), key: "q"))
    }

    // MARK: - Actions

    @objc func doSync() { Task { await syncNow() } }

    @objc func doPause(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let duration = Pause.Duration(rawValue: raw) else { return }
        Pause.starting(duration, at: Date()).save()
        refresh()
    }

    @objc func doResume() {
        Pause.none.save()
        refresh()
        Task { await syncNow() }
    }

    @objc func openConsole() {
        guard let url = config?.consoleReviewURL else { return }
        NSWorkspace.shared.open(url)
    }

    @objc func openConfig() {
        NSWorkspace.shared.open(Config.defaultPath)
    }

    @objc func quit() { NSApp.terminate(nil) }
}

extension AppDelegate: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) { buildMenu() }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// Accessory, so there is no Dock icon and no menu bar of its own — this is a
// status item, not a window.
app.setActivationPolicy(.accessory)
app.run()
