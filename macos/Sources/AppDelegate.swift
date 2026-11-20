// SPDX-License-Identifier: GPL-3.0-or-later
import AppKit
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    static weak var shared: AppDelegate?

    let engine = RelayEngine()
    private var statusItem: NSStatusItem!
    private var window: ActivityWindowController!
    private let menu = NSMenu()

    // MARK: launch

    func applicationWillFinishLaunching(_ notification: Notification) {
        AppDelegate.shared = self
        NSAppleEventManager.shared().setEventHandler(self, andSelector: #selector(handleURL(_:reply:)),
                                                     forEventClass: AEEventClass(kInternetEventClass),
                                                     andEventID: AEEventID(kAEGetURL))
        applyDockPolicy()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A second copy would fight the first over the phone link and the URL schemes.
        let me = ProcessInfo.processInfo.processIdentifier
        if let id = Bundle.main.bundleIdentifier,
           let other = NSRunningApplication.runningApplications(withBundleIdentifier: id).first(where: { $0.processIdentifier != me }) {
            other.activate(options: [])
            NSApp.terminate(nil)
            return
        }

        // Make sure macOS knows this app, even when it was started from a build folder or a terminal;
        // without that it refuses to route URLs here.
        LSRegisterURL(Bundle.main.bundleURL as CFURL, true)
        SchemeClaim.releaseStaleClaims()
        engine.verbose = CommandLine.arguments.contains("--verbose")
        installMainMenu()

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        menu.delegate = self
        statusItem.menu = menu
        window = ActivityWindowController(engine: engine)
        engine.onChange = { [weak self] in
            self?.updateIcon()
            self?.window.update()
        }
        updateIcon()
        engine.start()

        if CommandLine.arguments.contains("--print-menu") {
            // Debug aid: let the engine look at the phone, then print the menu as text and quit.
            DispatchQueue.main.asyncAfter(deadline: .now() + 6) {
                let (status, detail) = ActivityWindowController.describe(self.engine)
                print("STATUS: \(status)\nDETAIL: \(detail)")
                self.menuNeedsUpdate(self.menu)
                for item in self.menu.items {
                    let mark = item.state == .on ? "[x] " : ""
                    print(item.isSeparatorItem ? "  ----" : "  \(mark)\(item.attributedTitle?.string ?? item.title)\(item.submenu != nil ? " ▸" : "")")
                }
                fflush(stdout)
                NSApp.terminate(nil)
            }
            return
        }

        if !Prefs.hasLaunchedBefore {
            Prefs.hasLaunchedBefore = true
            window.show()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) { engine.shutdown() }

    /// Clicking the Dock icon (when shown) or reopening the app brings up the window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window?.show() }
        return true
    }

    @objc private func handleURL(_ event: NSAppleEventDescriptor, reply: NSAppleEventDescriptor) {
        guard let url = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue else { return }
        engine.receive(callback: url)
    }

    private func applyDockPolicy() {
        NSApp.setActivationPolicy(Prefs.showInDock ? .regular : .accessory)
    }

    // MARK: menu bar icon

    private func updateIcon() {
        guard let button = statusItem?.button else { return }
        var name = "MenuBarIcon"
        var dim = false
        switch engine.phase {
        case .working: name = "MenuBarIconActive"
        case .ready: break
        default: dim = true
        }
        if let image = NSImage(named: name) {
            image.size = NSSize(width: 18, height: 18)
            image.isTemplate = true
            button.image = image
        } else {
            button.title = "LR"
        }
        button.appearsDisabled = dim
        button.toolTip = ActivityWindowController.describe(engine).0
    }

    // MARK: menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let (status, detail) = ActivityWindowController.describe(engine)

        let good: Bool
        switch engine.phase { case .ready, .working: good = true; default: good = false }
        let head = NSMutableAttributedString(string: "●  ", attributes: [.foregroundColor: good ? NSColor.systemGreen : NSColor.systemOrange])
        head.append(NSAttributedString(string: status, attributes: [.font: NSFont.menuFont(ofSize: 0)]))
        let statusRow = NSMenuItem()
        statusRow.attributedTitle = head
        statusRow.isEnabled = false
        menu.addItem(statusRow)
        if !good, engine.busy == nil {
            let hint = NSMenuItem(title: detail, action: nil, keyEquivalent: "")
            hint.isEnabled = false
            hint.attributedTitle = NSAttributedString(string: detail, attributes: [
                .font: NSFont.menuFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor])
            menu.addItem(hint)
        }
        menu.addItem(.separator())

        if engine.busy == nil {
            switch engine.phase {
            case .noAdb:
                menu.addItem(item("Download Android Platform-Tools…", #selector(downloadPlatformTools)))
                menu.addItem(item("Choose adb…", #selector(pickAdb)))
            case .needsApp:
                if engine.bundledAPK != nil { menu.addItem(item("Install Login Relay on Phone", #selector(installOnPhone))) }
            case .ready:
                if engine.transport == .adb {
                    menu.addItem(item("Open Login Relay on Phone", #selector(openOnPhone)))
                    if engine.phoneAppIsOutdated, let have = engine.phoneVersion, let new = engine.bundledPhoneVersion {
                        menu.addItem(item("Update Login Relay on Phone (\(have) → \(new))", #selector(installOnPhone)))
                    }
                }
            case .working(let flow):
                menu.addItem(item("Cancel \(flow.request.verb)", #selector(cancelFlow)))
            case .noPhone, .unauthorized, .connecting:
                menu.addItem(item("Check Again", #selector(checkAgain)))
            }
        }

        let usbPhones = engine.transport == .usb ? engine.usbCandidates : []
        if usbPhones.count > 1 {
            let sub = NSMenu()
            for p in usbPhones {
                let it = NSMenuItem(title: p.name.isEmpty ? p.id : p.name, action: #selector(selectUsbPhone(_:)), keyEquivalent: "")
                it.target = self
                it.representedObject = p.id
                it.state = ("usb:" + p.id) == engine.device?.serial ? .on : .off
                sub.addItem(it)
            }
            let parent = NSMenuItem(title: "Phone", action: nil, keyEquivalent: "")
            parent.submenu = sub
            menu.addItem(parent)
        }

        let usable = engine.devices.filter { $0.isUsable }
        if usable.count > 1 {
            let sub = NSMenu()
            for d in usable {
                let it = NSMenuItem(title: "\(d.title) (\(d.serial))", action: #selector(selectPhone(_:)), keyEquivalent: "")
                it.target = self
                it.representedObject = d.serial
                it.state = d == engine.device ? .on : .off
                sub.addItem(it)
            }
            let parent = NSMenuItem(title: "Phone", action: nil, keyEquivalent: "")
            parent.submenu = sub
            menu.addItem(parent)
        }
        if menu.items.last?.isSeparatorItem == false { menu.addItem(.separator()) }

        let connectMenu = NSMenu()
        for (t, title) in [(Transport.adb, "adb (needs USB debugging)"), (Transport.usb, "USB accessory (no USB debugging) — beta")] {
            let it = NSMenuItem(title: title, action: #selector(chooseTransport(_:)), keyEquivalent: "")
            it.target = self
            it.representedObject = t.rawValue
            it.state = engine.transport == t ? .on : .off
            connectMenu.addItem(it)
        }
        let connectParent = NSMenuItem(title: "Connect Over", action: nil, keyEquivalent: "")
        connectParent.submenu = connectMenu
        menu.addItem(connectParent)

        menu.addItem(item("Show Activity…", #selector(showWindow)))
        menu.addItem(.separator())

        let login = item("Launch at Login", #selector(toggleLaunchAtLogin))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        let dock = item("Show in Dock", #selector(toggleDock))
        dock.state = Prefs.showInDock ? .on : .off
        menu.addItem(dock)
        let notes = item("Notify When a Sign-in Arrives", #selector(toggleNotifications))
        notes.state = Prefs.notifications ? .on : .off
        menu.addItem(notes)
        menu.addItem(.separator())

        menu.addItem(item("About Login Relay", #selector(about)))
        let quit = item("Quit Login Relay", #selector(NSApplication.terminate(_:)), key: "q")
        quit.target = NSApp
        menu.addItem(quit)
    }

    private func item(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.target = self
        return i
    }

    // MARK: actions

    @objc private func showWindow() { window.show() }
    @objc private func installOnPhone() { engine.installPhoneApp() }
    @objc private func openOnPhone() { engine.openOnPhone() }
    @objc private func checkAgain() { engine.checkAgain() }
    @objc private func cancelFlow() { engine.cancelActive("Cancelled on the computer.", notifyPhone: true) }
    @objc private func downloadPlatformTools() { confirmDownloadPlatformTools() }
    @objc private func pickAdb() { chooseAdb() }
    @objc private func chooseTransport(_ sender: NSMenuItem) {
        if let raw = sender.representedObject as? String, let t = Transport(rawValue: raw) { engine.setTransport(t) }
    }
    @objc private func selectUsbPhone(_ sender: NSMenuItem) {
        if let id = sender.representedObject as? String { engine.selectUsb(id) }
    }
    @objc private func selectPhone(_ sender: NSMenuItem) {
        if let serial = sender.representedObject as? String { engine.selectDevice(serial) }
    }

    func confirmDownloadPlatformTools() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Download Android platform-tools?"
        alert.informativeText = "This downloads Google's platform-tools (which include adb) directly from dl.google.com, checks them against Google's published checksum, and keeps them in Login Relay's own folder. They're covered by the Android SDK License."
        alert.addButton(withTitle: "Download")
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "View License")
        switch alert.runModal() {
        case .alertFirstButtonReturn: engine.downloadPlatformTools()
        case .alertThirdButtonReturn: NSWorkspace.shared.open(AdbInstaller.licenseURL)
        default: break
        }
    }

    func chooseAdb() {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.message = "Choose the adb program"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.directoryURL = URL(fileURLWithPath: NSHomeDirectory())
        if panel.runModal() == .OK, let url = panel.url { engine.chooseAdb(path: url.path) }
    }

    @objc private func toggleLaunchAtLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch {
            engine.showError("Couldn't change Launch at Login",
                             "\(error.localizedDescription)\n\nMove Login Relay to your Applications folder and try again.")
        }
    }

    @objc private func toggleDock() {
        Prefs.showInDock.toggle()
        applyDockPolicy()
        if Prefs.showInDock { NSApp.activate(ignoringOtherApps: true) }
    }

    @objc private func toggleNotifications() {
        Prefs.notifications.toggle()
        if Prefs.notifications { engine.notify(title: "Login Relay", body: "You'll see a note here when an app asks to sign in.") }
    }

    @objc private func about() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [
            .credits: NSAttributedString(string: "Finishes browser sign-ins for Android phones that have no browser, using this Mac's browser over adb.\n\nGPL-3.0-or-later",
                                         attributes: [.font: NSFont.systemFont(ofSize: 11)]),
        ])
    }

    // MARK: main menu (gives the window and alerts their standard keyboard shortcuts)

    private func installMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let app = NSMenu(); appItem.submenu = app
        app.addItem(withTitle: "About Login Relay", action: #selector(about), keyEquivalent: "").target = self
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Login Relay", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(withTitle: "Quit Login Relay", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit"); editItem.submenu = edit
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        let windowItem = NSMenuItem(); main.addItem(windowItem)
        let win = NSMenu(title: "Window"); windowItem.submenu = win
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        NSApp.mainMenu = main
    }
}
