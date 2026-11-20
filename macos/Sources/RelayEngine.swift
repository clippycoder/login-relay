// SPDX-License-Identifier: GPL-3.0-or-later
import AppKit
import UserNotifications

/// One sign-in (or sign-out) in progress.
final class Flow {
    let request: RelayRequest
    let link: PhoneConnection
    var claim: SchemeClaim?
    var timeout: Timer?

    init(request: RelayRequest, link: PhoneConnection) {
        self.request = request
        self.link = link
    }
}

enum Phase {
    case noPhone
    /// A phone is plugged in but Login Relay on it isn't answering yet. The text says what we're waiting for.
    case connecting(Device, String)
    case ready(Device)
    case working(Flow)
}

/// Watches for a phone, keeps a link to the Login Relay app on it, and runs each login: opens the
/// page in the browser, catches the redirect, and hands it back to the phone.
///
/// State lives on the main thread. A background thread watches the USB bus and owns the link.
final class RelayEngine {
    static let loginTimeout: TimeInterval = 15 * 60

    /// Test hook: LOGINRELAY_FAKE_PHONE=host:port makes the engine skip IOKit and behave as if exactly one
    /// phone ("Fake Phone") were plugged in, whose accessory pipe is a plain TCP connection to host:port.
    static var fakePhoneAddress: String? {
        guard let v = ProcessInfo.processInfo.environment["LOGINRELAY_FAKE_PHONE"], !v.isEmpty else { return nil }
        return v
    }

    // MARK: state (main thread)
    private(set) var device: Device?
    private(set) var linked = false
    private(set) var active: Flow?
    private(set) var logLines: [(date: Date, text: String)] = []
    private(set) var usbCandidates: [UsbDevice] = []
    private(set) var usbDetail = ""
    private(set) var usbAlive = false
    /// Login Relay on the phone has answered at least once on this connection. Android reclaims an idle app
    /// process on a small phone, which silences it; that's normal, and the next sign-in starts it again by itself.
    private var usbEverAlive = false
    private var awaitingDelivery: Flow?
    private var deliveryTimer: Timer?

    var onChange: (() -> Void)?
    var verbose = false

    var phase: Phase {
        guard let device else { return .noPhone }
        if let active { return .working(active) }
        if usbAlive || usbEverAlive { return .ready(device) }
        return .connecting(device, usbDetail)
    }

    // MARK: background loop state
    private var stopping = false
    private var generation = 0                 // bumped to abandon a stuck loop thread
    private var lastTick = Date()              // last time the loop made progress
    private var watchdog: Timer?
    private var forceCheck = false
    private let wake = DispatchSemaphore(value: 0)
    private var currentLink: PhoneConnection?
    private var thread: Thread?
    private let stateLock = NSLock()

    // MARK: lifecycle

    func start() {
        startLoop()
        // If the polling thread ever stops making progress (a wedged USB call, a missed wake-up), say so
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
        let t = Thread { [weak self] in
            if let fake = RelayEngine.fakePhoneAddress { self?.fakeLoop(gen: gen, address: fake) } else { self?.usbLoop(gen: gen) }
        }
        t.name = "relay-loop"
        t.start()
        thread = t
    }

    func shutdown() {
        stopping = true
        cancelActive("Quit", notifyPhone: true)
        currentLink?.close()
        wake.signal()
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
        let line = "[\(ISO8601DateFormatter().string(from: Date()))] \(text)\n"
        if verbose { fputs(line, stderr) }
        // Test hook: LOGINRELAY_LOG=<file> appends every log line (CI shows it when a test fails).
        if let path = ProcessInfo.processInfo.environment["LOGINRELAY_LOG"], !path.isEmpty {
            if !FileManager.default.fileExists(atPath: path) { FileManager.default.createFile(atPath: path, contents: nil) }
            if let h = FileHandle(forWritingAtPath: path) {
                h.seekToEndOfFile()
                h.write(Data(line.utf8))
                try? h.close()
            }
        }
        onChange?()
    }

    private func logAsync(_ text: String) { DispatchQueue.main.async { self.log(text) } }

    // MARK: background loop

    /// Progress marker for the watchdog. Written from the loop thread directly (not via the main
    /// thread), so a long modal dialog on the main thread can't make a healthy loop look stuck.
    private func tick() {
        stateLock.lock(); lastTick = Date(); stateLock.unlock()
    }

    private var secondsSinceTick: TimeInterval {
        stateLock.lock(); defer { stateLock.unlock() }
        return Date().timeIntervalSince(lastTick)
    }

    private func linkOpened(_ link: PhoneConnection) {
        currentLink = link
        linked = true
        log("USB connection open.")
        onChange?()
    }

    private func linkClosed(_ link: PhoneConnection) {
        if currentLink === link {
            currentLink = nil
            linked = false
            usbAlive = false
            usbEverAlive = false
            log("Link to the phone closed.")
        }
        if active?.link === link { cancelActive("The phone closed Login Relay.", notifyPhone: false) }
        onChange?()
    }

    // MARK: messages from the phone

    private func handle(_ msg: [String: Any], link: PhoneConnection) {
        switch msg["type"] as? String {
        case "open":
            guard let url = msg["url"] as? String else { return }
            let request = RelayRequest(url: url, referrer: msg["referrer"] as? String, label: msg["label"] as? String)
            DispatchQueue.main.async { self.begin(request, link: link) }
        case "cancel":
            DispatchQueue.main.async {
                if self.active?.link === link { self.cancelActive("Cancelled on the phone.", notifyPhone: false) }
            }
        case "hello":
            DispatchQueue.main.async {
                let version = msg["version"] as? String
                self.log("Login Relay \(version ?? "?") on the phone says hello (\(msg["transport"] as? String ?? "?")).")
            }
        case "delivered":
            DispatchQueue.main.async { self.deliveryConfirmed() }
        case "error":
            DispatchQueue.main.async { self.phoneReportedError(msg["text"] as? String ?? "unknown error") }
        default:
            break
        }
    }

    // MARK: one login

    private func begin(_ request: RelayRequest, link: PhoneConnection) {
        if active != nil { cancelActive("Replaced by a newer request.", notifyPhone: false) }
        log("\(request.appName) wants to open a \(request.verb) page\(request.host.map { " at \($0)" } ?? "").")

        if !isWebURL(request.url) {
            log("Refused to open a link that isn't a web page.")
            link.send(["type": "error", "text": "That kind of link can't be opened on the computer."])
            return
        }

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

        let flow = Flow(request: request, link: link)
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
        // The computer doesn't deliver anything itself: the phone checks the result and hands it to the app.
        awaitingDelivery = flow
        deliveryTimer?.invalidate()
        deliveryTimer = Timer.scheduledTimer(withTimeInterval: 10, repeats: false) { [weak self] _ in
            guard let self, self.awaitingDelivery != nil else { return }
            self.awaitingDelivery = nil
            self.log("The phone didn't confirm it handed the result to the app.")
        }
        flow.link.send(["type": "callback", "url": callback])
    }

    private func deliveryConfirmed() {
        guard let flow = awaitingDelivery else { return }
        awaitingDelivery = nil
        deliveryTimer?.invalidate()
        log("Done: \(flow.request.appName) is finishing the \(flow.request.verb).")
    }

    private func phoneReportedError(_ text: String) {
        log("The phone says: \(text)")
        if awaitingDelivery != nil {
            awaitingDelivery = nil
            deliveryTimer?.invalidate()
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

    // MARK: USB accessory mode

    func selectUsb(_ id: String) {
        Prefs.usbSelected = id
        refreshNow()
    }

    private func applyUSB(device: Device?, candidates: [UsbDevice], detail: String) {
        let changed = self.device != device || usbCandidates != candidates || usbDetail != detail
        if self.device != device { log(device.map { "USB phone: \($0.title)" } ?? "No USB phone.") }
        self.device = device
        usbCandidates = candidates
        usbDetail = detail
        if changed { onChange?() }
        if device == nil, active != nil { cancelActive("Phone not available", notifyPhone: false) }
    }

    private func usbAliveChanged(_ alive: Bool, link: PhoneConnection) {
        guard currentLink === link, usbAlive != alive else { return }
        usbAlive = alive
        if alive { usbEverAlive = true }
        log(alive ? "Login Relay on the phone is answering."
                  : "Login Relay on the phone went idle (it starts again by itself when an app asks to sign in).")
        if !alive {
            if active != nil { cancelActive("Login Relay on the phone stopped answering.", notifyPhone: false) }
        }
        onChange?()
    }

    /// Background loop for USB accessory mode: find a phone, switch it to accessory mode, open the pipe,
    /// and keep it open until the cable comes out.
    private func usbLoop(gen: Int) {
        let hostName = Host.current().localizedName ?? ProcessInfo.processInfo.hostName
        var lastSwitch: [UInt32: Date] = [:]
        func current() -> Bool { !stopping && gen == generation }
        func pause(_ seconds: TimeInterval) { _ = wake.wait(timeout: .now() + seconds) }
        func publish(_ d: Device?, _ c: [UsbDevice], _ detail: String) { publishAsync(gen, d, c, detail) }

        while current() {
            tick()
            let all = Usb.list()
            // A phone that was unplugged gets a fresh try when it comes back.
            for location in Array(lastSwitch.keys) where !all.contains(where: { $0.location == location }) {
                lastSwitch[location] = nil
            }
            let candidates = all.filter { $0.looksAndroid && !$0.isAccessory }
            var target = all.first(where: { $0.isAccessory })
            if target == nil {
                if let selected = Prefs.usbSelected, let c = candidates.first(where: { $0.id == selected }) {
                    target = c
                } else if candidates.count == 1 {
                    target = candidates[0]
                }
            }
            guard let t = target else {
                publish(nil, candidates, candidates.count > 1 ? "Choose which phone to use from the menu." : "")
                pause(1)
                continue
            }
            let device = Device(id: "usb:" + t.id, title: t.name.isEmpty ? t.id : t.name)

            if !t.isAccessory {
                if let last = lastSwitch[t.location], Date().timeIntervalSince(last) < 20 { pause(1); continue }
                lastSwitch[t.location] = Date()
                publish(device, candidates, "Asking \(t.name) to switch to USB accessory mode…")
                let r = Usb.switchToAccessory(location: t.location, hostName: hostName)
                if r.code != 0 {
                    publish(device, candidates, Usb.errorText(r, name: t.name))
                    pause(3)
                    continue
                }
                // Android gives up if the host hasn't reconnected within 10 s.
                var appeared = false
                for _ in 0..<60 {
                    guard current() else { return }
                    usleep(200_000)
                    tick()
                    if Usb.list().contains(where: { $0.location == t.location && $0.isAccessory }) { appeared = true; break }
                }
                if !appeared {
                    publish(device, candidates, "The phone didn't switch to accessory mode.")
                    continue
                }
            }

            var opened: OpaquePointer?
            for attempt in 0..<6 {
                guard current() else { return }
                let o = Usb.open(location: t.location)
                if let c = o.conn { opened = c; break }
                if attempt == 5 {
                    let detail = o.code == Int32(AOA_ERR_OPEN) && o.iokit == Usb.exclusiveAccess
                        ? "Another program has the phone open. Close it, then choose Check Again."
                        : "Couldn't open the accessory connection (error \(o.code))."
                    publish(device, candidates, detail)
                }
                usleep(500_000)
            }
            guard let conn = opened else { pause(3); continue }

            serve(AoaChannel(conn: conn), device: device, candidates: candidates, gen: gen) {
                Usb.list().contains(where: { $0.location == t.location && $0.isAccessory })
            }
        }
    }

    /// Test hook (`LOGINRELAY_FAKE_PHONE=host:port`): one phone, "Fake Phone", always plugged in; its pipe is TCP.
    private func fakeLoop(gen: Int, address: String) {
        let device = Device(id: "fake", title: "Fake Phone")
        while !stopping, gen == generation {
            tick()
            publishAsync(gen, device, [], "Waiting for the fake phone at \(address).")
            if let channel = SocketChannel.connect(address) {
                serve(channel, device: device, candidates: [], gen: gen) { true }
            }
            _ = wake.wait(timeout: .now() + 1)
        }
    }

    private func publishAsync(_ gen: Int, _ device: Device?, _ candidates: [UsbDevice], _ detail: String) {
        DispatchQueue.main.async { if gen == self.generation { self.applyUSB(device: device, candidates: candidates, detail: detail) } }
    }

    /// Runs the protocol over an open pipe until it ends (cable out, the phone hangs up, or the engine restarts).
    private func serve(_ channel: ByteChannel, device: Device, candidates: [UsbDevice], gen: Int, stillPresent: () -> Bool) {
        let link = PhoneLink(channel: channel)
        let closed = DispatchSemaphore(value: 0)
        link.onMessage = { [weak self, weak link] msg in
            guard let self, let link else { return }
            self.handle(msg, link: link)
        }
        link.onAlive = { [weak self, weak link] alive in
            DispatchQueue.main.async { if let link { self?.usbAliveChanged(alive, link: link) } }
        }
        link.onClose = { [weak self, weak link] in
            DispatchQueue.main.async { if let link { self?.linkClosed(link) } }
            closed.signal()
        }
        publishAsync(gen, device, candidates, "Login Relay on the phone isn't answering yet. If Android asks to allow USB access, tap OK; otherwise just start a sign-in on the phone and Login Relay starts by itself.")
        DispatchQueue.main.async { self.linkOpened(link) }
        link.start()
        while closed.wait(timeout: .now() + 1) == .timedOut {
            tick()
            if stopping || gen != generation || !stillPresent() { link.close(); break }
        }
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
