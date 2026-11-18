// SPDX-License-Identifier: GPL-3.0-or-later
import CoreServices
import Foundation

/// Makes this app the macOS handler for one URL scheme (an app's sign-in return address) for the
/// length of one login, then puts the previous handler back.
///
/// A running app can claim a scheme its Info.plist never declared, and then receives the URLs as
/// ordinary Apple events, so no helper app is needed. Claims are written to UserDefaults first, so
/// a crash mid-login is cleaned up on the next launch.
final class SchemeClaim {
    private static let defaultsKey = "activeSchemeClaims"  // [scheme: previous handler or ""]
    private static var ownID: String { Bundle.main.bundleIdentifier ?? "io.github.clippycoder.loginrelay" }

    let scheme: String
    private let previous: String?
    private var released = false

    /// Returns nil if macOS refuses the claim, so the caller can fall back to pasting the address.
    init?(scheme: String) {
        self.scheme = scheme
        let current = SchemeClaim.currentHandler(scheme)
        // A leftover claim from a crashed run isn't a "previous handler" worth restoring.
        previous = (current == SchemeClaim.ownID) ? nil : current
        var claims = UserDefaults.standard.dictionary(forKey: SchemeClaim.defaultsKey) as? [String: String] ?? [:]
        claims[scheme] = previous ?? ""
        UserDefaults.standard.set(claims, forKey: SchemeClaim.defaultsKey)
        let rc = LSSetDefaultHandlerForURLScheme(scheme as CFString, SchemeClaim.ownID as CFString)
        guard rc == noErr, SchemeClaim.currentHandler(scheme) == SchemeClaim.ownID else {
            SchemeClaim.restore(scheme: scheme, previous: previous)
            claims.removeValue(forKey: scheme)
            UserDefaults.standard.set(claims, forKey: SchemeClaim.defaultsKey)
            return nil
        }
    }

    func release() {
        guard !released else { return }
        released = true
        SchemeClaim.restore(scheme: scheme, previous: previous)
        var claims = UserDefaults.standard.dictionary(forKey: SchemeClaim.defaultsKey) as? [String: String] ?? [:]
        claims.removeValue(forKey: scheme)
        UserDefaults.standard.set(claims, forKey: SchemeClaim.defaultsKey)
    }

    /// Undoes claims left behind by a previous run that didn't get to clean up.
    static func releaseStaleClaims() {
        let claims = UserDefaults.standard.dictionary(forKey: defaultsKey) as? [String: String] ?? [:]
        for (scheme, previous) in claims where currentHandler(scheme) == ownID {
            restore(scheme: scheme, previous: previous.isEmpty ? nil : previous)
        }
        UserDefaults.standard.removeObject(forKey: defaultsKey)
    }

    private static func restore(scheme: String, previous: String?) {
        // An empty identifier clears the association when nothing handled the scheme before.
        LSSetDefaultHandlerForURLScheme(scheme as CFString, (previous ?? "") as CFString)
    }

    static func currentHandler(_ scheme: String) -> String? {
        LSCopyDefaultHandlerForURLScheme(scheme as CFString)?.takeRetainedValue() as String?
    }
}
