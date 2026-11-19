// SPDX-License-Identifier: GPL-3.0-or-later
import AppKit
import UserNotifications

/// One sign-in (or sign-out) in progress.
final class Flow {
    let request: RelayRequest
    let device: Device
    let link: PhoneLink
    var claim: SchemeClaim?
    var timeout: Timer?

    init(request: RelayRequest, device: Device, link: PhoneLink) {
        self.request = request
        self.device = device
        self.link = link
    }
}

enum Phase {
    case noAdb
    case noPhone
    case unauthorized(Device)
    case needsApp(Device)
    case ready(Device)
    case working(Flow)
}

/// Watches for a phone, keeps a link to the Login Relay app on it, and runs each login: opens the
/// page in the browser, catches the redirect, and hands it back to the phone.
///
/// State lives on the main thread. A background thread polls adb and owns the link.
final class RelayEngine {
    static let phonePackage = "io.github.clippycoder.oauthrelay"
    static let loginTimeout: TimeInterval = 15 * 60

    // MARK: state (main thread)
    private(set) var adb: Adb?
    private(set) var devices: [Device] = []
    private(set) var device: Device?
    private(set) var phoneVersion: String?
    private(set) var linked = false
    private(set) var active: Flow?
    private(set) var busy: String?
    private(set) var logLines: [(date: Date, text: String)] = []

    var onChange: (() -> Void)?
    var verbose = false

    var phase: Phase {
        if adb == nil { return .noAdb }
        guard let device else {
            if let blocked = devices.first(where: { !$0.isUsable }) { return .unauthorized(blocked) }
            return .noPhone
        }
        if phoneVersion == nil { return .needsApp(device) }
        if let active { return .working(active) }
        return .ready(device)
    }

    /// The version of the phone app bundled with this Mac app, if any.
    var bundledPhoneVersion: String? { Bundle.main.object(forInfoDictionaryKey: "LoginRelayPhoneAppVersion") as? String }
    var bundledAPK: URL? { Bundle.main.url(forResource: "login-relay", withExtension: "apk") }
    var phoneAppIsOutdated: Bool {
        guard let have = phoneVersion, let bundled = bundledPhoneVersion else { return false }
        return versionLess(have, bundled)
    }

    // MARK: background loop state
    private var stopping = false
    private var generation = 0                 // bumped to abandon a stuck loop thread
    private var lastTick = Date()              // last time the loop made progress
    private var watchdog: Timer?
    private var forceCheck = false
    private let wake = DispatchSemaphore(value: 0)
    private var currentLink: PhoneLink?
    private var thread: Thread?
    private let stateLock = NSLock()

    // MARK: lifecycle

    func start() {
        startLoop()
        // If the polling thread ever stops making progress (adb wedged, a missed wake-up), say so
        // and start over instead of showing a stale state forever.
        watchdog = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            guard let self, !self.stopping, self.secondsSinceTick > 30 else { return }
            self.log("Checking for the phone got stuck; starting over.")
            self.currentLink?.close()
            self.startLoop()
        }
        log("Started.")
    }

    private func startLoop() {
        generation += 1
        let gen = generation
        tick()
        let t = Thread { [weak self] in self?.loop(gen: gen) }
        t.name = "relay-loop"
        t.start()
        thread = t
    }

    func shutdown() {
        stopping = true
        cancelActive("Quit", notifyPhone: true)
        currentLink?.close()
        wake.signal()
        if let adb, let device {
            if let port = existingForward(adb, serial: device.serial) { adb.removeForward(port, serial: device.serial) }
        }
    }

    /// "Check Again" in the menu.
    func checkAgain() {
        log("Checking for the phone…")
        refreshNow()
    }

    func refreshNow() {
        stateLock.lock(); forceCheck = true; stateLock.unlock()
        wake.signal()
    }

    // MARK: log

    func log(_ text: String) {
        logLines.append((Date(), text))
        if logLines.count > 300 { logLines.removeFirst(logLines.count - 300) }
        if verbose { fputs("[\(ISO8601DateFormatter().string(from: Date()))] \(text)\n", stderr) }
        onChange?()
    }

    private func logAsync(_ text: String) { DispatchQueue.main.async { self.log(text) } }

    // MARK: background loop

    private func loop(gen: Int) {
        var lastCheck = Date.distantPast
        var port: UInt16?
        var ready: Device?
        var adb: Adb?
        while !stopping, gen == generation {
            tick()
            stateLock.lock()
            let due = forceCheck || Date().timeIntervalSince(lastCheck) >= 3
            forceCheck = false
            stateLock.unlock()

            if due {
                lastCheck = Date()
                let found = Adb.locate(userChoice: Prefs.adbPath).map(Adb.init(path:))
                adb = found
                var devs: [Device] = [], chosen: Device?, version: String?
                port = nil
                if let found {
                    devs = found.devices()
                    chosen = choose(devs)
                    if let chosen {
                        version = found.installedVersion(chosen.serial, package: RelayEngine.phonePackage)
                        if version != nil { port = ensureForward(found, serial: chosen.serial) }
                    }
                }
                ready = (version != nil && port != nil) ? chosen : nil
                DispatchQueue.main.async {
                    if gen == self.generation { self.publish(adb: found, devices: devs, device: chosen, version: version) }
                }
            }

            if let port, let ready {
                let link = PhoneLink()
                let closed = DispatchSemaphore(value: 0)
                link.onMessage = { [weak self, weak link] msg in
                    guard let self, let link else { return }
                    self.handle(msg, link: link, device: ready)
                }
                link.onClose = { [weak self, weak link] in
                    DispatchQueue.main.async { if let link { self?.linkClosed(link) } }
                    closed.signal()
                }
                if link.open(port: port) {
                    DispatchQueue.main.async { self.linkOpened(link) }
                    // adb can hold a connection open without it reaching the phone (for instance
                    // while a replugged phone is still reconnecting), so keep checking that the
                    // phone is really there rather than waiting for the link to end on its own.
                    while closed.wait(timeout: .now() + 3) == .timedOut {
                        tick()
                        if stopping || gen != generation { link.close(); break }
                        let here = adb?.devices() ?? []
                        if !here.contains(where: { $0.serial == ready.serial && $0.isUsable }) {
                            link.close()
                            break
                        }
                    }
                    lastCheck = .distantPast  // look at the phone again straight away
                    continue
                }
            }
            _ = wake.wait(timeout: .now() + 1)
        }
    }

    /// Progress marker for the watchdog. Written from the loop thread directly (not via the main
    /// thread), so a long modal dialog on the main thread can't make a healthy loop look stuck.
    private func tick() {
        stateLock.lock(); lastTick = Date(); stateLock.unlock()
    }

    private var secondsSinceTick: TimeInterval {
        stateLock.lock(); defer { stateLock.unlock() }
        return Date().timeIntervalSince(lastTick)
    }

    private func choose(_ devs: [Device]) -> Device? {
        let usable = devs.filter { $0.isUsable }
        if let s = Prefs.selectedSerial, let d = usable.first(where: { $0.serial == s }) { return d }
        return usable.first
    }

    /// Reuses an existing forward for this phone rather than adding a new one every few seconds.
    private func existingForward(_ adb: Adb, serial: String) -> UInt16? {
        let r = adb.run(["forward", "--list"])
        guard r.ok else { return nil }
        for line in r.out.split(separator: "\n") {
            let p = line.split(separator: " ").map(String.init)
            if p.count == 3, p[0] == serial, p[2] == "localabstract:oauthrelay", p[1].hasPrefix("tcp:") {
                return UInt16(p[1].dropFirst(4))
            }
        }
        return nil
    }

    private func ensureForward(_ adb: Adb, serial: String) -> UInt16? {
        existingForward(adb, serial: serial) ?? adb.forward(serial)
    }

    // MARK: main-thread publishing

    private func publish(adb: Adb?, devices: [Device], device: Device?, version: String?) {
        let changed = self.adb?.path != adb?.path || self.devices != devices
            || self.device != device || self.phoneVersion != version
        if self.adb?.path != adb?.path {
            log(adb == nil ? "adb not found." : "Using adb at \(adb!.path).")
        }
        if self.devices != devices, !devices.isEmpty {
            log("adb lists: " + devices.map { "\($0.serial) (\($0.state))" }.joined(separator: ", "))
        }
        if self.device != device {
            log(device.map { "Phone: \($0.title) (\($0.serial))" } ?? "No phone.")
        }
        if self.phoneVersion != version, device != nil {
            log(version.map { "Login Relay \($0) is on the phone." } ?? "Login Relay isn't on the phone.")
        }
        self.adb = adb
        self.devices = devices
        self.device = device
        self.phoneVersion = version
        if changed { onChange?() }
        if device == nil || version == nil, active != nil { cancelActive("Phone not available", notifyPhone: false) }
    }

    private func linkOpened(_ link: PhoneLink) {
        currentLink = link
        linked = true
        log("Linked to Login Relay on the phone.")
        onChange?()
    }

    private func linkClosed(_ link: PhoneLink) {
        if currentLink === link { currentLink = nil; linked = false; log("Link to the phone closed.") }
        if active?.link === link { cancelActive("The phone closed Login Relay.", notifyPhone: false) }
        onChange?()
    }

    // MARK: messages from the phone

    private func handle(_ msg: [String: Any], link: PhoneLink, device: Device) {
        switch msg["type"] as? String {
        case "open":
            guard let url = msg["url"] as? String else { return }
            let request = RelayRequest(url: url, referrer: msg["referrer"] as? String, label: msg["label"] as? String)
            DispatchQueue.main.async { self.begin(request, link: link, device: device) }
        case "cancel":
            DispatchQueue.main.async {
                if self.active?.link === link { self.cancelActive("Cancelled on the phone.", notifyPhone: false) }
            }
        default:
            break
        }
    }

    // MARK: one login

    private func begin(_ request: RelayRequest, link: PhoneLink, device: Device) {
        if active != nil { cancelActive("Replaced by a newer request.", notifyPhone: false) }
        log("\(request.appName) wants to open a \(request.verb) page\(request.host.map { " at \($0)" } ?? "").")

        if request.kind == .plainLink {
            if let url = URL(string: request.url) { NSWorkspace.shared.open(url) }
            link.send(["type": "status", "text": "Opened on your computer."])
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { link.send(["type": "done"]) }
            return
        }

        if request.lacksPKCE, !confirmWithoutPKCE(request) {
            log("Declined: the sign-in doesn't use PKCE.")
            link.send(["type": "error", "text": "Cancelled on the computer."])
            return
        }

        let flow = Flow(request: request, device: device, link: link)
        active = flow
        if let scheme = request.catchableScheme {
            flow.claim = SchemeClaim(scheme: scheme)
            if flow.claim == nil { log("macOS wouldn't let Login Relay take over \(scheme)://, so you'll paste the address instead.") }
        }
        flow.timeout = Timer.scheduledTimer(withTimeInterval: RelayEngine.loginTimeout, repeats: false) { [weak self, weak flow] _ in
            guard let self, let flow, self.active === flow else { return }
            flow.link.send(["type": "error", "text": "Timed out waiting for the browser."])
            self.cancelActive("Timed out waiting for the browser.", notifyPhone: true)
        }
        link.send(["type": "status",
                   "text": request.kind == .signIn ? "Finish signing in on your computer." : "Finishing sign-out on your computer."])
        // Test hook: the end-to-end tests set LOGINRELAY_BROWSER=none so no browser opens.
        if ProcessInfo.processInfo.environment["LOGINRELAY_BROWSER"] != "none", let url = URL(string: request.url) {
            NSWorkspace.shared.open(url)
        }
        log("Opened the page in your browser. If it asks to open Login Relay, allow it.")
        notify(title: "\(request.appName) \(request.verb)", body: "Continue in your browser.")
        onChange?()
        if flow.claim == nil {
            DispatchQueue.main.async { self.askForPastedAddress(flow) }
        }
    }

    /// Called with every URL macOS hands us. Only the one that matches the login in progress counts.
    func receive(callback: String) {
        guard let flow = active else { return }
        switch flow.request.accepts(callback: callback) {
        case .unrelated:
            return
        case .stateMismatch:
            log("Ignored a return address from a different sign-in (state mismatch).")
        case .ok:
            finish(flow, callback: callback)
        }
    }

    private func finish(_ flow: Flow, callback: String) {
        guard active === flow else { return }
        active = nil
        flow.timeout?.invalidate()
        flow.claim?.release()
        if let err = urlParams(callback)["error"] {
            log("The page returned an error: \(urlParams(callback)["error_description"] ?? err)")
        }
        log("Returning to \(flow.request.appName) on the phone…")
        onChange?()
        guard let adb else { return }
        DispatchQueue.global().async {
            let r = adb.deliver(flow.device.serial, url: callback, package: flow.request.caller)
            DispatchQueue.main.async {
                if !r.ok || r.out.contains("Error") {
                    self.log("The phone couldn't take the result: \(r.out.isEmpty ? r.err : r.out)")
                    flow.link.send(["type": "error", "text": "Couldn't hand the result back to the app."])
                    return
                }
                self.log("Done: \(flow.request.appName) is finishing the \(flow.request.verb).")
                // Let the app's own screen take the result before Login Relay closes.
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { flow.link.send(["type": "done"]) }
            }
        }
    }

    func cancelActive(_ reason: String, notifyPhone: Bool) {
        guard let flow = active else { return }
        active = nil
        flow.timeout?.invalidate()
        flow.claim?.release()
        log(reason)
        if notifyPhone { flow.link.send(["type": "done"]) }
        onChange?()
    }

    // MARK: prompts

    private func confirmWithoutPKCE(_ request: RelayRequest) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "\(request.appName) is signing in without PKCE"
        alert.informativeText = "The code that passes through this computer could be used on its own to take over the sign-in. Only continue if you trust this app and this computer."
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Continue Anyway")
        return alert.runModal() == .alertSecondButtonReturn
    }

    /// For return addresses that are ordinary web links, which macOS can't hand to us.
    private func askForPastedAddress(_ flow: Flow) {
        guard active === flow, let redirect = flow.request.redirect else { return }
        NSApp.activate(ignoringOtherApps: true)
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24))
        field.placeholderString = redirect
        let alert = NSAlert()
        alert.messageText = "Paste the address you ended up on"
        alert.informativeText = "After you sign in, copy the full address from your browser's address bar. It starts with \(redirect.split(separator: "?").first.map(String.init) ?? redirect)."
        alert.accessoryView = field
        alert.addButton(withTitle: "Send to Phone")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        while active === flow {
            guard alert.runModal() == .alertFirstButtonReturn else {
                cancelActive("Cancelled on the computer.", notifyPhone: true)
                return
            }
            let pasted = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            switch flow.request.accepts(callback: pasted) {
            case .ok: finish(flow, callback: pasted); return
            case .stateMismatch: alert.informativeText = "That address is from a different sign-in. Try again."
            case .unrelated: alert.informativeText = "That isn't this app's return address. Try again."
            }
        }
    }

    // MARK: phone-side app management

    func installPhoneApp() {
        guard let adb, let device, let apk = bundledAPK else { return }
        busy = "Installing Login Relay on \(device.title)…"
        log(busy!)
        onChange?()
        DispatchQueue.global().async {
            let r = adb.install(device.serial, apk: apk.path)
            DispatchQueue.main.async {
                self.busy = nil
                if r.ok && r.out.contains("Success") {
                    self.log("Installed.")
                } else {
                    let why = [r.out, r.err].filter { !$0.isEmpty }.joined(separator: " ")
                    self.log("Install failed: \(why)")
                    self.showError("Couldn't install Login Relay on the phone", why)
                }
                self.onChange?()
                self.refreshNow()
            }
        }
    }

    func openOnPhone() {
        guard let adb, let device else { return }
        DispatchQueue.global().async {
            _ = adb.shell(device.serial, "input keyevent KEYCODE_WAKEUP; am start -n \(RelayEngine.phonePackage)/.RelayActivity")
        }
    }

    func downloadPlatformTools() {
        busy = "Downloading Android platform-tools…"
        log(busy!)
        onChange?()
        DispatchQueue.global().async {
            var failure: Error?
            do { try AdbInstaller.install { step in self.logAsync(step) } } catch { failure = error }
            DispatchQueue.main.async {
                self.busy = nil
                if let failure {
                    self.log("Download failed: \(failure.localizedDescription)")
                    self.showError("Couldn't download platform-tools", failure.localizedDescription)
                }
                self.onChange?()
                self.refreshNow()
            }
        }
    }

    func selectDevice(_ serial: String) {
        Prefs.selectedSerial = serial
        refreshNow()
    }

    func chooseAdb(path: String) {
        Prefs.adbPath = path
        refreshNow()
    }

    // MARK: notifications and alerts

    func notify(title: String, body: String) {
        guard Prefs.notifications else { return }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }

    func showError(_ title: String, _ detail: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = detail
        alert.runModal()
    }
}
