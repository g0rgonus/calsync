import AppKit
import CalsyncMirrorCore
import CalsyncMirrorEventKit

/// The settings window.
///
/// It exists because the alternative was handing `config.json` to
/// `NSWorkspace.open`, which on a developer's Mac means Xcode and on anybody
/// else's means whatever claimed `.json` last. The two things it fixes are not
/// cosmetic: a destination calendar typed as text is a silent no-op the moment
/// it is misspelt — `calendar(titled:)` throws once per run into a log nobody
/// opens — and the console link does nothing at all until `apiURL` is set, with
/// the config file being exactly where somebody would go to set it.
///
/// Nothing here decides anything. Every rule it enforces — the guard ceiling,
/// the holding pens, two pairs aimed at one calendar, what a usable address
/// looks like — is `ConfigDraft` in `CalsyncMirrorCore`, tested without a Mac
/// and without a permission prompt. This file is boxes, and where they sit.
final class SettingsWindow: NSObject, NSWindowDelegate {

    /// Called after a save has landed on disk, so the app can re-read it.
    private let onSave: () -> Void
    private let path: URL

    private var window: NSWindow?
    private var text: [ConfigField: NSTextField] = [:]
    private var rows: [PairRow] = []
    private var rowStack: NSStackView!
    private var accessNotice: NSTextField!
    private var accessButton: NSButton!
    private var problems: NSTextField!
    private var choices = CalendarStore.CalendarChoices(titles: [], granted: false)

    init(path: URL = Config.defaultPath, onSave: @escaping () -> Void) {
        self.path = path
        self.onSave = onSave
    }

    // MARK: - Showing

    /// Bring the window up, and the app with it.
    ///
    /// `LSUIElement` is the trap here. The app runs as `.accessory`, which is
    /// what keeps it out of the Dock and gives it a status item instead of a
    /// menu bar — and an accessory app has no menu bar at all, so ⌘V in the API
    /// token field does nothing. Pasting a token is most of what this window is
    /// for. So it becomes `.regular` while the window is open, which brings a
    /// real menu bar (below) and lets it come properly to the front, and drops
    /// back to `.accessory` in `windowWillClose`. Permanently regular would put
    /// a Dock icon on a background tool, which is the thing `LSUIElement` is
    /// there to prevent.
    ///
    /// `activate(ignoringOtherApps:)` is needed on top of
    /// `makeKeyAndOrderFront`: the click that opened this came from a status
    /// menu, not from the app being frontmost, so without it the window is
    /// ordered in behind whatever the user was actually looking at and takes no
    /// keystrokes.
    func show() {
        if window == nil { build() }
        reloadCalendars()
        Self.installMainMenu()
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        // Back to a status item with no Dock presence. Deferred by one turn of
        // the run loop because changing the policy while the window is still
        // being torn down leaves the previous app un-activated, and the desktop
        // rather than the user's editor comes forward.
        DispatchQueue.main.async { NSApp.setActivationPolicy(.accessory) }
    }

    /// A `.regular` app with no main menu shows an empty menu bar, and — more
    /// to the point — nothing routes ⌘X/⌘C/⌘V to the first responder. This is
    /// the smallest menu that makes the text fields behave like text fields.
    private static func installMainMenu() {
        guard NSApp.mainMenu == nil else { return }
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "Hide Calsync Mirror",
                                   action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "Quit Calsync Mirror",
                                   action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let edit = NSMenu(title: "Edit")
        edit.addItem(NSMenuItem(title: "Undo", action: Selector(("undo:")), keyEquivalent: "z"))
        edit.addItem(NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "Z"))
        edit.addItem(.separator())
        edit.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        edit.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        edit.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        edit.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)),
                                keyEquivalent: "a"))
        editItem.submenu = edit
        main.addItem(editItem)

        NSApp.mainMenu = main
    }

    // MARK: - Building

    private func build() {
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 18
        content.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)

        content.addArrangedSubview(section("Radicale", grid([
            ("Address", field(.radicaleURL, width: 340,
                              hint: "http://homebox:5232/calsync — the principal, "
                                  + "not the server root")),
            ("Username", field(.username, width: 200,
                               hint: "not needed if the deployment allows anonymous reads")),
            ("Password", field(.password, width: 200, secure: true)),
        ])))

        content.addArrangedSubview(section("Calendars", pairsEditor()))

        content.addArrangedSubview(section("calsync API", grid([
            ("Address", field(.apiURL, width: 340,
                              hint: "https://homebox/v1 — switches on the review badge "
                                  + "and the console link")),
            ("Token", field(.apiToken, width: 340, secure: true)),
        ])))

        content.addArrangedSubview(section("Schedule", grid([
            ("Sync every", field(.syncIntervalMinutes, width: 70, hint: "minutes")),
            ("Give up after", field(.timeoutSeconds, width: 70,
                                    hint: "seconds — Radicale is on a LAN or a tailnet; "
                                        + "it answers quickly or it is not there")),
            ("Say so when offline for", field(.offlineWarnAfterHours, width: 70,
                                              hint: "hours — below this, being away is "
                                                  + "unremarkable and stays quiet")),
        ])))

        content.addArrangedSubview(section("Safety", grid([
            ("Hold deletions past", field(.maxDisappearancePct, width: 70,
                                          hint: "of tracked future events (0.2 = 20%)")),
            ("…or past", field(.maxDisappearanceCount, width: 70,
                               hint: "events vanishing in one run")),
            ("Mirror from", field(.windowBackDays, width: 70, hint: "days back")),
            ("…to", field(.windowForwardDays, width: 70, hint: "days ahead")),
        ]), note: "Absence is the only cancellation signal here — a partial read looks "
            + "exactly like a called-off season. These may be tightened freely and "
            + "cannot be widened past "
            + "\(ConfigLimits.disappearancePct.upperBound) or "
            + "\(ConfigLimits.disappearanceCount.upperBound)."))

        problems = wrapping("", width: 520)
        problems.textColor = .systemRed
        problems.isHidden = true
        content.addArrangedSubview(problems)

        content.addArrangedSubview(buttons())

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 720),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered, defer: false)
        window.title = "Calsync Mirror Settings"
        // Closing must not deallocate it — this object holds the only other
        // reference, and reopening should be the same window in the same place.
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = content
        window.setContentSize(content.fittingSize)
        window.center()
        self.window = window

        load()
    }

    /// A heading, whatever sits under it, and an optional line of reasoning.
    private func section(_ title: String, _ body: NSView, note: String? = nil) -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8

        let heading = NSTextField(labelWithString: title)
        heading.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        stack.addArrangedSubview(heading)
        stack.addArrangedSubview(body)
        if let note {
            let label = wrapping(note, width: 500)
            label.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            label.textColor = .secondaryLabelColor
            stack.addArrangedSubview(label)
        }
        return stack
    }

    /// A label and whatever it labels, in two columns that line up.
    private func grid(_ definitions: [(String, NSView)]) -> NSGridView {
        let grid = NSGridView(numberOfColumns: 2, rows: 0)
        grid.rowSpacing = 8
        grid.columnSpacing = 10
        grid.column(at: 0).xPlacement = .trailing
        grid.rowAlignment = .firstBaseline
        for (label, control) in definitions {
            grid.addRow(with: [NSTextField(labelWithString: label), control])
        }
        return grid
    }

    private func field(
        _ which: ConfigField, width: CGFloat, secure: Bool = false, hint: String? = nil
    ) -> NSView {
        let box: NSTextField = secure ? NSSecureTextField() : NSTextField()
        box.translatesAutoresizingMaskIntoConstraints = false
        box.widthAnchor.constraint(equalToConstant: width).isActive = true
        box.font = .systemFont(ofSize: NSFont.systemFontSize)
        text[which] = box
        guard let hint else { return box }

        let row = NSStackView(views: [box])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 8
        let label = wrapping(hint, width: 300)
        label.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        label.textColor = .secondaryLabelColor
        row.addArrangedSubview(label)
        return row
    }

    private func wrapping(_ string: String, width: CGFloat) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: string)
        label.translatesAutoresizingMaskIntoConstraints = false
        // Both: the constraint stops it stretching the window, and the max
        // layout width is what makes it choose a second line instead.
        label.preferredMaxLayoutWidth = width
        label.widthAnchor.constraint(lessThanOrEqualToConstant: width).isActive = true
        label.isSelectable = false
        return label
    }

    private func buttons() -> NSView {
        let reveal = NSButton(title: "Reveal Config in Finder",
                              target: self, action: #selector(revealConfig))
        reveal.bezelStyle = .rounded
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancel))
        cancel.bezelStyle = .rounded
        cancel.keyEquivalent = "\u{1b}"
        let save = NSButton(title: "Save", target: self, action: #selector(save))
        save.bezelStyle = .rounded
        save.keyEquivalent = "\r"

        let bar = NSStackView(views: [reveal, spacer, cancel, save])
        bar.orientation = .horizontal
        bar.spacing = 10
        bar.translatesAutoresizingMaskIntoConstraints = false
        bar.widthAnchor.constraint(equalToConstant: 520).isActive = true
        return bar
    }

    // MARK: - The pair editor

    /// One row of the collection → calendar table.
    private final class PairRow {
        let collection = NSTextField()
        let calendar = NSPopUpButton()
        let remove: NSButton
        let view: NSStackView
        private var chosen: String

        init(pair: Pair, target: AnyObject, removeAction: Selector) {
            chosen = pair.calendar
            collection.stringValue = pair.collection
            collection.placeholderString = "games"
            collection.translatesAutoresizingMaskIntoConstraints = false
            collection.widthAnchor.constraint(equalToConstant: 150).isActive = true

            calendar.translatesAutoresizingMaskIntoConstraints = false
            calendar.widthAnchor.constraint(equalToConstant: 260).isActive = true

            remove = NSButton(title: "−", target: target, action: removeAction)
            remove.bezelStyle = .rounded
            remove.setContentHuggingPriority(.required, for: .horizontal)

            let arrow = NSTextField(labelWithString: "→")
            arrow.textColor = .secondaryLabelColor
            view = NSStackView(views: [collection, arrow, calendar, remove])
            view.orientation = .horizontal
            view.alignment = .firstBaseline
            view.spacing = 8
        }

        /// Which calendar this row is asking for, kept apart from the popup's
        /// own state.
        ///
        /// The menu is rebuilt every time the calendar list is re-read, and a
        /// rebuild forgets what was selected. Remembering the *intent* here is
        /// what stops a Mac that has not been granted calendar access — which
        /// can list nothing, so every popup is empty — from silently rewriting
        /// a working config to no destination at all.
        var calendarTitle: String {
            if let title = calendar.titleOfSelectedItem, !title.isEmpty { chosen = title }
            return chosen
        }

        /// What the row is asking for. A title is the whole of it —
        /// `Pair.calendar` is a title and `calendar(titled:)` matches on one.
        var pair: Pair {
            Pair(collection: collection.stringValue, calendar: calendarTitle)
        }
    }

    private func pairsEditor() -> NSView {
        rowStack = NSStackView()
        rowStack.orientation = .vertical
        rowStack.alignment = .leading
        rowStack.spacing = 6

        accessNotice = wrapping("", width: 500)
        accessNotice.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        accessNotice.isHidden = true
        accessButton = NSButton(title: "Request Calendar Access…",
                                target: self, action: #selector(requestAccess))
        accessButton.bezelStyle = .rounded
        accessButton.isHidden = true

        let add = NSButton(title: "Add", target: self, action: #selector(addPair))
        add.bezelStyle = .rounded

        let note = wrapping(
            "Only the collections calsync writes for the family belong here. "
            + "\(ConfigDraft.holdingPens.sorted().joined(separator: " and ")) are "
            + "holding pens for events calsync could not place — they are meant to "
            + "wait in /review, not land on everybody's phone.", width: 500)
        note.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        note.textColor = .secondaryLabelColor

        let stack = NSStackView(views: [rowStack, add, accessNotice, accessButton, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        return stack
    }

    /// Ask EventKit what is on this Mac, and rebuild every popup around the
    /// answer.
    ///
    /// A configured calendar that is not in the list is *kept* rather than
    /// dropped — a Mac that has not been granted access, or an account that is
    /// temporarily signed out, must not quietly rewrite a working config — and
    /// it is called out underneath instead.
    private func reloadCalendars() {
        choices = CalendarStore().choices()
        for row in rows { populate(row) }

        let missing = rows
            .map { $0.pair.calendar }
            .filter { !$0.isEmpty && !choices.titles.contains($0) }

        if !choices.granted {
            accessNotice.stringValue =
                "Calendar access has not been granted, so this Mac cannot say which "
                + "calendars exist. Anything already configured is kept and shown "
                + "below. Grant access and the pickers fill in."
            accessNotice.textColor = .systemOrange
            accessNotice.isHidden = false
            accessButton.isHidden = false
        } else if !missing.isEmpty {
            accessNotice.stringValue =
                "Not a calendar on this Mac: "
                + missing.map { $0.debugDescription }.joined(separator: ", ")
                + ". A run would fail on that pair every interval rather than write "
                + "anything, so pick an existing calendar or create it in Calendar.app."
            accessNotice.textColor = .systemOrange
            accessNotice.isHidden = false
            accessButton.isHidden = true
        } else {
            accessNotice.isHidden = true
            accessButton.isHidden = true
        }
    }

    private func populate(_ row: PairRow) {
        let current = row.calendarTitle
        row.calendar.removeAllItems()
        var titles = choices.titles
        // Whatever is configured stays selectable even when EventKit does not
        // list it; the notice above says why it is not there.
        if !current.isEmpty, !titles.contains(current) { titles.insert(current, at: 0) }
        if titles.isEmpty {
            row.calendar.addItem(withTitle: "")
            row.calendar.isEnabled = false
        } else {
            row.calendar.addItems(withTitles: titles)
            row.calendar.isEnabled = true
            row.calendar.selectItem(withTitle: current)
        }
    }

    private func addRow(_ pair: Pair) {
        let row = PairRow(pair: pair, target: self, removeAction: #selector(removePair))
        rows.append(row)
        rowStack.addArrangedSubview(row.view)
        populate(row)
    }

    // MARK: - Loading and saving

    private func load() {
        let config = (try? Config.load(from: path)) ?? Config(radicaleURL: "", pairs: [])
        let draft = ConfigDraft(config)
        text[.radicaleURL]?.stringValue = draft.radicaleURL
        text[.username]?.stringValue = draft.username
        text[.password]?.stringValue = draft.password
        text[.apiURL]?.stringValue = draft.apiURL
        text[.apiToken]?.stringValue = draft.apiToken
        text[.syncIntervalMinutes]?.stringValue = draft.syncIntervalMinutes
        text[.timeoutSeconds]?.stringValue = draft.timeoutSeconds
        text[.offlineWarnAfterHours]?.stringValue = draft.offlineWarnAfterHours
        text[.maxDisappearancePct]?.stringValue = draft.maxDisappearancePct
        text[.maxDisappearanceCount]?.stringValue = draft.maxDisappearanceCount
        text[.windowBackDays]?.stringValue = draft.windowBackDays
        text[.windowForwardDays]?.stringValue = draft.windowForwardDays
        for pair in draft.pairs { addRow(pair) }
    }

    private var draft: ConfigDraft {
        var draft = ConfigDraft()
        draft.radicaleURL = text[.radicaleURL]?.stringValue ?? ""
        draft.username = text[.username]?.stringValue ?? ""
        draft.password = text[.password]?.stringValue ?? ""
        draft.apiURL = text[.apiURL]?.stringValue ?? ""
        draft.apiToken = text[.apiToken]?.stringValue ?? ""
        draft.syncIntervalMinutes = text[.syncIntervalMinutes]?.stringValue ?? ""
        draft.timeoutSeconds = text[.timeoutSeconds]?.stringValue ?? ""
        draft.offlineWarnAfterHours = text[.offlineWarnAfterHours]?.stringValue ?? ""
        draft.maxDisappearancePct = text[.maxDisappearancePct]?.stringValue ?? ""
        draft.maxDisappearanceCount = text[.maxDisappearanceCount]?.stringValue ?? ""
        draft.windowBackDays = text[.windowBackDays]?.stringValue ?? ""
        draft.windowForwardDays = text[.windowForwardDays]?.stringValue ?? ""
        draft.pairs = rows.map(\.pair)
        return draft
    }

    @objc private func save() {
        for box in text.values { box.backgroundColor = .textBackgroundColor }
        do {
            let config = try draft.resolve()
            // `Config.write` chmods the file 0600 itself, because it can hold a
            // calendar password. Nothing here reimplements that.
            try config.write(to: path)
            problems.isHidden = true
            onSave()
            window?.close()
        } catch let refusal as ConfigRefused {
            for problem in refusal.problems {
                text[problem.field]?.backgroundColor =
                    NSColor.systemRed.withAlphaComponent(0.12)
            }
            report(refusal.problems.map(\.message))
        } catch {
            // Writing failed — a read-only home directory, a full disk. Say so
            // and leave the window open with everything still in it, rather
            // than closing on the assumption it worked.
            report(["Could not write \(path.path): \(error)"])
        }
    }

    private func report(_ messages: [String]) {
        problems.stringValue = messages.map { "• " + $0 }.joined(separator: "\n")
        problems.isHidden = false
        window?.setContentSize(window?.contentView?.fittingSize ?? .zero)
    }

    // MARK: - Actions

    @objc private func addPair() {
        addRow(Pair(collection: "", calendar: ""))
        window?.setContentSize(window?.contentView?.fittingSize ?? .zero)
    }

    @objc private func removePair(_ sender: NSButton) {
        guard let index = rows.firstIndex(where: { $0.remove === sender }) else { return }
        rowStack.removeArrangedSubview(rows[index].view)
        rows[index].view.removeFromSuperview()
        rows.remove(at: index)
        window?.setContentSize(window?.contentView?.fittingSize ?? .zero)
    }

    /// The one place this window asks for anything. It is a button rather than
    /// something the window does on opening: a permission dialog nobody asked
    /// for is how people learn to hit Don't Allow.
    @objc private func requestAccess() {
        Task { @MainActor in
            try? await CalendarStore().requestAccess()
            reloadCalendars()
        }
    }

    @objc private func cancel() { window?.close() }

    /// `selectFile`, never `open`. Opening a `.json` hands it to whatever
    /// claimed the extension — Xcode, on the machine this was written on —
    /// which is the behaviour this window replaced.
    @objc private func revealConfig() {
        if FileManager.default.fileExists(atPath: path.path) {
            NSWorkspace.shared.selectFile(
                path.path, inFileViewerRootedAtPath: path.deletingLastPathComponent().path)
        } else {
            // Nothing written yet — show the directory it will land in.
            NSWorkspace.shared.selectFile(
                nil, inFileViewerRootedAtPath: path.deletingLastPathComponent().path)
        }
    }
}
