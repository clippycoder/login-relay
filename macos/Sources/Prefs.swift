// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

/// How the computer reaches the phone.
enum Transport: String {
    /// adb over USB debugging (or wireless debugging).
    case adb
    /// USB accessory mode: no USB debugging needed.
    case usb
}

enum Prefs {
    private static let d = UserDefaults.standard

    /// An adb the user picked by hand.
    static var adbPath: String? {
        get { d.string(forKey: "adbPath") }
        set { d.set(newValue, forKey: "adbPath") }
    }

    /// Which phone to use when several are connected.
    static var selectedSerial: String? {
        get { d.string(forKey: "selectedSerial") }
        set { d.set(newValue, forKey: "selectedSerial") }
    }

    /// Off (the default) = menu bar only, no Dock icon. On = also a Dock icon.
    static var showInDock: Bool {
        get { d.bool(forKey: "showInDock") }
        set { d.set(newValue, forKey: "showInDock") }
    }

    static var notifications: Bool {
        get { d.bool(forKey: "notifications") }
        set { d.set(newValue, forKey: "notifications") }
    }

    static var hasLaunchedBefore: Bool {
        get { d.bool(forKey: "hasLaunchedBefore") }
        set { d.set(newValue, forKey: "hasLaunchedBefore") }
    }

    static var transport: Transport {
        // Test hook: LOGINRELAY_TRANSPORT=adb|usb overrides the saved choice (the end-to-end tests use adb).
        get {
            if let forced = ProcessInfo.processInfo.environment["LOGINRELAY_TRANSPORT"], let t = Transport(rawValue: forced) { return t }
            return Transport(rawValue: d.string(forKey: "transport") ?? "") ?? .adb
        }
        set { d.set(newValue.rawValue, forKey: "transport") }
    }

    /// Which USB phone to use when several are plugged in (a location id, as hex).
    static var usbSelected: String? {
        get { d.string(forKey: "usbSelected") }
        set { d.set(newValue, forKey: "usbSelected") }
    }
}
