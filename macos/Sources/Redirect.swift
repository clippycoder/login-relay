// SPDX-License-Identifier: GPL-3.0-or-later
// Pure logic, no AppKit: classifies a page the phone wants opened and decides what to wait for.
// Mirrors host/oauth_relay.py; macos/Tests/run.sh covers it.
import Foundation

enum RequestKind {
    case signIn
    case signOut
    case plainLink
}

/// Query and fragment parameters of a URL, percent-decoded; the first value wins.
func urlParams(_ url: String) -> [String: String] {
    var result: [String: String] = [:]
    var rest = Substring(url)
    var fragment = Substring("")
    if let hash = rest.firstIndex(of: "#") {
        fragment = rest[rest.index(after: hash)...]
        rest = rest[..<hash]
    }
    var query = Substring("")
    if let q = rest.firstIndex(of: "?") {
        query = rest[rest.index(after: q)...]
    }
    for chunk in [query, fragment] {
        for pair in chunk.split(separator: "&", omittingEmptySubsequences: true) {
            let kv = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            let key = decode(String(kv[0]))
            let value = kv.count > 1 ? decode(String(kv[1])) : ""
            if result[key] == nil { result[key] = value }
        }
    }
    return result
}

private func decode(_ s: String) -> String {
    let spaced = s.replacingOccurrences(of: "+", with: " ")
    return spaced.removingPercentEncoding ?? spaced
}

/// Everything before ? or # (the part that identifies a redirect target).
private func base(_ url: String) -> String {
    var end = url.endIndex
    if let i = url.firstIndex(of: "?") { end = min(end, i) }
    if let i = url.firstIndex(of: "#") { end = min(end, i) }
    return String(url[..<end])
}

/// True if `callback` is the redirect URI plus an optional query or fragment. The scheme compares
/// case-insensitively and a trailing slash doesn't matter.
func sameRedirect(_ callback: String, _ redirect: String) -> Bool {
    func split(_ s: String) -> (scheme: String, rest: String)? {
        guard let colon = base(s).firstIndex(of: ":") else { return nil }
        let b = base(s)
        var rest = String(b[b.index(after: colon)...])
        while rest.hasSuffix("/") { rest.removeLast() }
        return (String(b[..<colon]).lowercased(), rest)
    }
    guard let a = split(callback), let b = split(redirect) else { return false }
    return a.scheme == b.scheme && a.rest == b.rest
}

/// Only web pages are opened in the browser; a file: or custom-scheme address from the phone could
/// otherwise launch an arbitrary program on the computer.
func isWebURL(_ url: String) -> Bool {
    let s = urlScheme(url)
    return s == "http" || s == "https"
}

func urlScheme(_ url: String) -> String? {
    guard let colon = url.firstIndex(of: ":") else { return nil }
    let s = String(url[..<colon]).lowercased()
    return s.isEmpty ? nil : s
}

struct RelayRequest {
    let url: String
    let referrer: String?
    /// Package name of the app that asked for the page, from "android-app://<package>".
    let caller: String?
    /// Display name the phone resolved for that app, if the phone app is new enough to send one.
    let label: String?
    let kind: RequestKind
    /// Where the browser will send the user back to the app.
    let redirect: String?
    let state: String?
    let responseType: String
    let hasPKCE: Bool
    let host: String?

    init(url: String, referrer: String?, label: String? = nil) {
        let p = urlParams(url)
        self.url = url
        self.referrer = referrer
        self.label = (label?.isEmpty ?? true) ? nil : label
        if let r = referrer, r.hasPrefix("android-app://") {
            let pkg = r.dropFirst("android-app://".count).split(separator: "/").first.map(String.init)
            self.caller = (pkg?.isEmpty ?? true) ? nil : pkg
        } else {
            self.caller = nil
        }
        // Sign-in uses redirect_uri. Sign-out uses post_logout_redirect_uri (OpenID Connect) or
        // returnTo (Auth0's /v2/logout).
        if let r = p["redirect_uri"] {
            kind = .signIn
            redirect = r
        } else if let r = p["post_logout_redirect_uri"] ?? p["returnTo"] {
            kind = .signOut
            redirect = r
        } else {
            kind = .plainLink
            redirect = nil
        }
        state = p["state"]
        responseType = p["response_type"] ?? "code"
        hasPKCE = p["code_challenge"] != nil
        host = URL(string: url)?.host
    }

    /// An authorization-code sign-in without PKCE hands a redeemable code through this computer.
    var lacksPKCE: Bool {
        kind == .signIn && responseType.contains("code") && !hasPKCE
    }

    /// A custom scheme the Mac can claim for one login. http(s) return addresses (App Links) can't
    /// be intercepted this way, so those fall back to pasting the final address.
    var catchableScheme: String? {
        guard let r = redirect, let s = urlScheme(r), s != "http", s != "https" else { return nil }
        return s
    }

    /// Short name for menus and notifications.
    var appName: String { label ?? caller ?? "An app" }

    var verb: String {
        switch kind {
        case .signIn: return "sign-in"
        case .signOut: return "sign-out"
        case .plainLink: return "link"
        }
    }

    /// Does this address, arriving at the Mac, belong to this request?
    func accepts(callback: String) -> CallbackVerdict {
        guard let redirect else { return .unrelated }
        guard sameRedirect(callback, redirect) else { return .unrelated }
        if let state, urlParams(callback)["state"] != state { return .stateMismatch }
        return .ok
    }
}

enum CallbackVerdict {
    case ok
    case unrelated
    case stateMismatch
}

/// Quotes a string for the phone's shell (`adb shell` joins its arguments and re-parses them).
func shellQuote(_ s: String) -> String {
    "'" + s.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
}

/// Dotted-number comparison: "0.2.2" < "0.3.0".
func versionLess(_ a: String, _ b: String) -> Bool {
    let x = a.split(separator: ".").map { Int($0) ?? 0 }, y = b.split(separator: ".").map { Int($0) ?? 0 }
    for i in 0..<max(x.count, y.count) {
        let l = i < x.count ? x[i] : 0, r = i < y.count ? y[i] : 0
        if l != r { return l < r }
    }
    return false
}
