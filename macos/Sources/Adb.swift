// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

struct Device: Equatable {
    let serial: String
    /// "device", "unauthorized", "offline", ...
    let state: String
    let model: String

    var isUsable: Bool { state == "device" }
    var title: String { model.isEmpty ? serial : model.replacingOccurrences(of: "_", with: " ") }
}

struct ProcessResult {
    let status: Int32
    let out: String
    let err: String
    var ok: Bool { status == 0 }
}

/// Thin wrapper over the adb command-line tool. Everything here blocks; call it off the main thread.
final class Adb {
    let path: String

    init(path: String) { self.path = path }

    // MARK: finding adb

    /// GUI apps start with a minimal PATH, so look in the usual places explicitly.
    static func locate(userChoice: String?) -> String? {
        let home = NSHomeDirectory()
        var candidates: [String] = []
        // Test hook: the end-to-end tests (tests/e2e) point the app at a fake adb.
        if let forced = ProcessInfo.processInfo.environment["LOGINRELAY_ADB"], !forced.isEmpty { return forced }
        if let userChoice { candidates.append(userChoice) }
        candidates.append(AdbInstaller.installedPath)
        for key in ["ANDROID_HOME", "ANDROID_SDK_ROOT"] {
            if let sdk = ProcessInfo.processInfo.environment[key] { candidates.append(sdk + "/platform-tools/adb") }
        }
        candidates += [
            home + "/Library/Android/sdk/platform-tools/adb",
            "/opt/homebrew/bin/adb",
            "/usr/local/bin/adb",
            "/opt/local/bin/adb",
        ]
        let fm = FileManager.default
        return candidates.first { fm.isExecutableFile(atPath: $0) }
    }

    // MARK: running

    @discardableResult
    func run(_ args: [String], serial: String? = nil, timeout: TimeInterval = 20) -> ProcessResult {
        Adb.exec(path, (serial.map { ["-s", $0] } ?? []) + args, timeout: timeout)
    }

    static func exec(_ launchPath: String, _ args: [String], timeout: TimeInterval = 20) -> ProcessResult {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launchPath)
        p.arguments = args
        p.standardInput = FileHandle.nullDevice
        // Output goes to temp files, not pipes. When adb has to start its server it forks a daemon
        // that inherits our stdout/stderr; with pipes we'd wait for end-of-file until that daemon
        // exits, i.e. forever.
        let fm = FileManager.default
        let id = UUID().uuidString
        let outURL = fm.temporaryDirectory.appendingPathComponent("loginrelay-\(id).out")
        let errURL = fm.temporaryDirectory.appendingPathComponent("loginrelay-\(id).err")
        fm.createFile(atPath: outURL.path, contents: nil)
        fm.createFile(atPath: errURL.path, contents: nil)
        defer { try? fm.removeItem(at: outURL); try? fm.removeItem(at: errURL) }
        guard let outH = try? FileHandle(forWritingTo: outURL), let errH = try? FileHandle(forWritingTo: errURL) else {
            return ProcessResult(status: -1, out: "", err: "couldn't create temp files")
        }
        p.standardOutput = outH
        p.standardError = errH
        do { try p.run() } catch {
            return ProcessResult(status: -1, out: "", err: "\(error)")
        }
        let killer = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
        p.waitUntilExit()
        killer.cancel()
        try? outH.close()
        try? errH.close()
        func text(_ url: URL) -> String {
            String(decoding: (try? Data(contentsOf: url)) ?? Data(), as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return ProcessResult(status: p.terminationStatus, out: text(outURL), err: text(errURL))
    }

    // MARK: phone queries

    func devices() -> [Device] {
        let r = run(["devices", "-l"], timeout: 15)
        guard r.ok else { return [] }
        return r.out.split(separator: "\n").compactMap { line in
            if line.hasPrefix("*") || line.hasPrefix("List of devices") { return nil }
            let parts = line.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)
            guard parts.count >= 2 else { return nil }
            let model = parts.first { $0.hasPrefix("model:") }.map { String($0.dropFirst(6)) } ?? ""
            return Device(serial: parts[0], state: parts[1], model: model)
        }
    }

    func shell(_ serial: String, _ command: String, timeout: TimeInterval = 20) -> ProcessResult {
        run(["shell", command], serial: serial, timeout: timeout)
    }

    /// Version name of the phone-side app, or nil if it isn't installed.
    func installedVersion(_ serial: String, package: String) -> String? {
        let r = shell(serial, "dumpsys package \(package)")
        guard r.ok, r.out.contains("versionName=") else { return nil }
        for line in r.out.split(separator: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("versionName=") { return String(t.dropFirst("versionName=".count)) }
        }
        return nil
    }

    /// Sets up the tunnel to the phone app's abstract socket and returns the local TCP port.
    func forward(_ serial: String) -> UInt16? {
        let r = run(["forward", "tcp:0", "localabstract:oauthrelay"], serial: serial)
        return r.ok ? UInt16(r.out) : nil
    }

    func removeForward(_ port: UInt16, serial: String) {
        run(["forward", "--remove", "tcp:\(port)"], serial: serial)
    }

    /// Hands a return address to the app that asked for the page.
    func deliver(_ serial: String, url: String, package: String?) -> ProcessResult {
        var cmd = "am start -a android.intent.action.VIEW -d \(shellQuote(url))"
        if let package { cmd += " -p \(shellQuote(package))" }
        return shell(serial, cmd)
    }

    func install(_ serial: String, apk: String) -> ProcessResult {
        run(["install", "-r", apk], serial: serial, timeout: 120)
    }
}
