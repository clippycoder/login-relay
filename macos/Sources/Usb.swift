// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

/// A phone as the engine sees it: a USB device, or the fake one the test hook provides.
struct Device: Equatable {
    /// "usb:<location>" for a real phone.
    let id: String
    let title: String
}

struct UsbDevice: Equatable {
    let vendorID: UInt16
    let productID: UInt16
    let location: UInt32
    /// Already in accessory mode (Google 18D1:2D00...2D05).
    let isAccessory: Bool
    /// Exposes an MTP/PTP or debugging interface, so it's worth asking about accessory mode.
    let looksAndroid: Bool
    let name: String
    let serial: String

    var id: String { String(format: "%08x", location) }
}

enum Usb {
    /// kIOReturnExclusiveAccess: another program has the device open.
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
            return "Another program has the phone open. Close it, then choose Check Again."
        case Int32(AOA_ERR_OPEN): return "Couldn't open \(name) over USB."
        case Int32(AOA_ERR_NOT_FOUND): return "\(name) disappeared."
        default: return "Couldn't switch \(name) to accessory mode (error \(r.code))."
        }
    }
}

/// The accessory pipe (bulk IN/OUT) as a `ByteChannel`.
final class AoaChannel: ByteChannel {
    private let conn: OpaquePointer
    let padsBulkPackets = true

    init(conn: OpaquePointer) { self.conn = conn }

    func read(into buffer: UnsafeMutablePointer<UInt8>, capacity: Int, timeoutMs: Int) -> Int {
        Int(aoa_read(conn, buffer, UInt32(capacity), UInt32(timeoutMs)))
    }

    func write(_ bytes: UnsafePointer<UInt8>, count: Int) -> Bool {
        aoa_write(conn, bytes, UInt32(count), 500) > 0
    }

    func close() { aoa_close(conn) }
}
