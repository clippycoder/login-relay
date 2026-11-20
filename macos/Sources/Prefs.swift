// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

enum Prefs {
    private static let d = UserDefaults.standard

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

    /// Which USB phone to use when several are plugged in (a location id, as hex).
    static var usbSelected: String? {
        get { d.string(forKey: "usbSelected") }
        set { d.set(newValue, forKey: "usbSelected") }
    }
}
