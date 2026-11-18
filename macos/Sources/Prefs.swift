// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

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
}
