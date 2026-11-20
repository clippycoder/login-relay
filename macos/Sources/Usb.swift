// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

/// What the engine needs from a connection to the phone, whichever way it's wired.
protocol PhoneConnection: AnyObject {
    func send(_ object: [String: Any])
    func close()
}

extension PhoneLink: PhoneConnection {}

struct UsbDevice: Equatable {
    let vendorID: UInt16
    let productID: UInt16
    let location: UInt32
    /// Already in accessory mode (Google 18D1:2D00...2D05).
    let isAccessory: Bool
    /// Exposes an MTP/PTP/ADB-style interface, so it's worth asking about accessory mode.
    let looksAndroid: Bool
    let name: String
    let serial: String

    var id: String { String(format: "%08x", location) }
}

enum Usb {
    /// kIOReturnExclusiveAccess: another program (usually the adb server) has the device open.
    static let exclusiveAccess: UInt32 = 0xe00002c5

    static func list() -> [UsbDevice] {
        var infos = [aoa_info](repeating: aoa_info(), count: 64)
        let n = Int(aoa_list(&infos, 64))
        return infos.prefix(n).map { info in
            var name = info.name, serial = info.serial
            return UsbDevice(vendorID: info.vendor_id, productID: info.product_id, location: info.location_id,
                             isAccessory: info.is_accessory != 0, looksAndroid: info.looks_android != 0,
                             name: text(&name), serial: text(&serial))
        }
    }

    private static func text<T>(_ buffer: inout T) -> String {
        withUnsafeBytes(of: &buffer) { raw in
            let end = raw.firstIndex(of: 0) ?? raw.count
            return String(decoding: raw[..<end], as: UTF8.self)
        }
    }

    struct SwitchResult {
        let code: Int32
        let protocolVersion: Int32
        let iokit: UInt32
    }

    /// Asks the phone to switch to accessory mode. The strings must match res/xml/accessory_filter.xml
    /// in the phone app ("Login Relay" / "Desktop"); the description is shown on the phone.
    static func switchToAccessory(location: UInt32, hostName: String) -> SwitchResult {
        let strings = ["Login Relay", "Desktop", hostName, "1",
                       "https://github.com/clippycoder/login-relay", String(format: "%08x", location)]
        let owned = strings.map { strdup($0)! }
        defer { owned.forEach { free($0) } }
        let pointers: [UnsafePointer<CChar>?] = owned.map { UnsafePointer($0) }
        var proto: Int32 = 0
        let code = aoa_switch(location, pointers, &proto)
        return SwitchResult(code: code, protocolVersion: proto, iokit: aoa_last_iokit_error())
    }

    static func open(location: UInt32) -> (conn: OpaquePointer?, code: Int32, iokit: UInt32) {
        var code: Int32 = 0
        let conn = aoa_open(location, &code)
        return (conn, code, aoa_last_iokit_error())
    }

    static func errorText(_ r: SwitchResult, name: String) -> String {
        switch r.code {
        case Int32(AOA_ERR_UNSUPPORTED): return "\(name) doesn't answer the USB accessory protocol."
        case Int32(AOA_ERR_OPEN) where r.iokit == exclusiveAccess:
            return "Another program has \(name) open (usually adb)."
        case Int32(AOA_ERR_OPEN): return "Couldn't open \(name) over USB."
        case Int32(AOA_ERR_NOT_FOUND): return "\(name) disappeared."
        default: return "Couldn't switch \(name) to accessory mode (error \(r.code))."
        }
    }
}

/// The connection to the Login Relay app on the phone over a USB accessory pipe. Same line-delimited
/// JSON as the adb link. Bulk USB has no "connection closed", so the link pings the phone and tracks
/// whether anything has come back recently ("alive").
final class UsbPhoneLink: PhoneConnection {
    var onMessage: (([String: Any]) -> Void)?
    var onClose: (() -> Void)?
    /// Called on the read thread when the phone app starts or stops answering.
    var onAlive: ((Bool) -> Void)?

    private static let silence: TimeInterval = 8
    private let conn: OpaquePointer
    private let writeLock = NSLock()
    private var closed = false          // guarded by writeLock
    private let stopLock = NSLock()
    private var stopping = false        // guarded by stopLock

    init(conn: OpaquePointer) { self.conn = conn }

    func start() {
        let t = Thread { [weak self] in self?.readLoop() }
        t.name = "usb-link"
        t.start()
    }

    func send(_ object: [String: Any]) {
        guard let json = try? JSONSerialization.data(withJSONObject: object) else { return }
        let frame = usbFrame(json)
        writeLock.lock()
        defer { writeLock.unlock() }
        if closed { return }
        frame.withUnsafeBytes { raw in
            guard let base = raw.bindMemory(to: UInt8.self).baseAddress else { return }
            var offset = 0
            while offset < frame.count {
                let chunk = min(frame.count - offset, 16384)
                // A timeout means the phone app isn't reading; drop the message. Liveness covers the rest.
                if aoa_write(conn, base + offset, UInt32(chunk), 500) <= 0 { return }
                offset += chunk
            }
        }
    }

    /// Ends the link; the read thread notices within a fraction of a second.
    func close() {
        stopLock.lock(); stopping = true; stopLock.unlock()
    }

    private var isStopping: Bool {
        stopLock.lock(); defer { stopLock.unlock() }
        return stopping
    }

    private func readLoop() {
        var buffer = [UInt8](repeating: 0, count: 16384)
        var splitter = LineSplitter()
        var lastHeard = Date.distantPast
        var lastPing = Date.distantPast
        var alive = false
        defer {
            writeLock.lock()
            closed = true
            aoa_close(conn)
            writeLock.unlock()
            onClose?()
        }
        while !isStopping {
            let n = aoa_read(conn, &buffer, UInt32(buffer.count), 200)
            if n < 0 { return }
            if n > 0 {
                lastHeard = Date()
                guard let lines = splitter.feed(Data(buffer[0..<Int(n)])) else { return }
                for line in lines {
                    if let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] { onMessage?(obj) }
                }
            }
            if Date().timeIntervalSince(lastPing) > 2 {
                send(["type": "ping"])
                lastPing = Date()
            }
            let nowAlive = Date().timeIntervalSince(lastHeard) < UsbPhoneLink.silence
            if nowAlive != alive {
                alive = nowAlive
                onAlive?(alive)
            }
        }
    }
}
