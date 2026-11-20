// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

var failures = 0
func check(_ cond: Bool, _ name: String, file: String = #file, line: Int = #line) {
    if !cond { failures += 1; print("FAIL \(name) (line \(line))") }
}

let auth = "https://login.example.com/authorize?client_id=x&response_type=code&state=S%201&code_challenge=abc" +
    "&redirect_uri=com.example.app%3A%2F%2Flogin.example.com%2Fandroid%2Fcallback"
let r = RelayRequest(url: auth, referrer: "android-app://com.example.app", label: "Example")
check(r.kind == .signIn, "kind signIn")
check(r.redirect == "com.example.app://login.example.com/android/callback", "redirect decoded")
check(r.state == "S 1", "state decoded")
check(r.caller == "com.example.app", "caller")
check(r.appName == "Example", "label wins")
check(r.catchableScheme == "com.example.app", "scheme")
check(!r.lacksPKCE, "has PKCE")
check(r.host == "login.example.com", "host")

let ok = "com.example.app://login.example.com/android/callback?code=abc&state=S%201"
check(r.accepts(callback: ok) == .ok, "accepts good callback")
check(r.accepts(callback: "COM.EXAMPLE.APP://login.example.com/android/callback/?code=1&state=S%201") == .ok,
      "scheme case + trailing slash")
check(r.accepts(callback: "com.example.app://login.example.com/android/callback?code=1&state=other") == .stateMismatch,
      "state mismatch")
check(r.accepts(callback: "com.example.app://login.example.com/android/callback?code=1") == .stateMismatch,
      "missing state")
check(r.accepts(callback: "com.other.app://login.example.com/android/callback?state=S%201") == .unrelated,
      "other scheme")
check(r.accepts(callback: "com.example.app://elsewhere/callback?state=S%201") == .unrelated, "other path")

// Auth0 sign-out
let out = RelayRequest(url: "https://login.example.com/v2/logout?client_id=y&returnTo=com.example.app%3A%2F%2Flogin.example.com%2Fandroid%2Fcallback",
                       referrer: "android-app://com.example.app")
check(out.kind == .signOut, "signOut via returnTo")
check(out.state == nil && out.accepts(callback: "com.example.app://login.example.com/android/callback") == .ok,
      "signOut callback needs no state")
check(!out.lacksPKCE, "signOut never lacks PKCE")
let oidc = RelayRequest(url: "https://idp.example.com/logout?post_logout_redirect_uri=myapp%3A%2Fdone",
                        referrer: "android-app://my.app")
check(oidc.kind == .signOut && oidc.catchableScheme == "myapp", "OIDC post_logout_redirect_uri")

// no PKCE
let weak = RelayRequest(url: "https://x.example/authorize?response_type=code&redirect_uri=app%3A%2F%2Fcb", referrer: nil)
check(weak.lacksPKCE, "code flow without PKCE is flagged")
let implicit = RelayRequest(url: "https://x.example/authorize?response_type=token&redirect_uri=app%3A%2F%2Fcb", referrer: nil)
check(!implicit.lacksPKCE, "implicit flow isn't a PKCE concern")

// App Links redirect: not catchable
let links = RelayRequest(url: "https://x.example/authorize?redirect_uri=https%3A%2F%2Fapp.example%2Fcb&code_challenge=z",
                         referrer: "android-app://a.b")
check(links.catchableScheme == nil, "https redirect falls back to paste")
check(links.accepts(callback: "https://app.example/cb?code=1") == .ok, "https callback matches when pasted")

// plain link and odd referrers
let plain = RelayRequest(url: "https://example.com/terms", referrer: "android-app://com.a.b/something")
check(plain.kind == .plainLink && plain.caller == "com.a.b" && plain.redirect == nil, "plain link")
check(RelayRequest(url: "https://example.com", referrer: nil).caller == nil, "no referrer")
check(RelayRequest(url: "https://example.com", referrer: "https://evil.example").caller == nil, "non-app referrer")
check(RelayRequest(url: "https://example.com", referrer: nil, label: "").label == nil, "empty label ignored")
check(RelayRequest(url: "https://example.com", referrer: nil).appName == "An app", "default app name")

// fragment params (implicit flow returns)
check(urlParams("app://cb#access_token=t&state=s")["state"] == "s", "fragment params")
check(urlParams("app://cb?a=1&a=2")["a"] == "1", "first value wins")

// shell quoting
check(shellQuote("a b") == "'a b'", "quote simple")
check(shellQuote("it's") == "'it'\"'\"'s'", "quote apostrophe")
check(shellQuote("x;rm -rf /") == "'x;rm -rf /'", "quote metachars")

// USB framing
let frame = { (n: Int) in usbFrame(Data(repeating: 0x41, count: n)) }
check(frame(10).count == 11 && frame(10).last == 0x0A, "frame adds a newline")
check(frame(63).count == 65, "63 + newline = 64 bytes needs a pad")
check(frame(64).count == 65 && frame(64)[64] == 0x0A, "64 + newline = 65, no pad needed")
check(frame(511).count == 513, "511 + newline = 512 bytes gets padded")
check(frame(1023).count == 1025, "1023 + newline = 1024 bytes gets padded")
check(frame(62).count == 63, "no padding when not a multiple of 64")
var splitter = LineSplitter()
check(splitter.feed(Data("{\"a\":1}\n{\"b\"".utf8))?.count == 1, "first line complete, second held")
let second = splitter.feed(Data(":2}\n\n\n{\"c\":3}\n".utf8))
check(second?.count == 2 && String(decoding: second![0], as: UTF8.self) == "{\"b\":2}", "held bytes joined, blank lines skipped")
var broken = LineSplitter()
check(broken.feed(Data(repeating: 0x41, count: LineSplitter.maxLine + 1)) == nil, "runaway line is rejected")

// only web pages get opened
check(isWebURL("https://example.com/a?b=c") && isWebURL("HTTP://example.com"), "web URLs")
check(!isWebURL("file:///etc/passwd") && !isWebURL("com.example.app://cb") && !isWebURL("javascript:alert(1)") && !isWebURL("no scheme"), "non-web URLs refused")

// version comparison
check(versionLess("0.2.2", "0.3.0"), "0.2.2 < 0.3.0")
check(!versionLess("0.3.0", "0.3.0"), "equal isn't less")
check(!versionLess("0.10.0", "0.9.9"), "numeric, not lexical")
check(versionLess("0.3", "0.3.1"), "shorter is padded with zeros")
check(!versionLess("1.0.0", "0.99.99"), "major wins")

// ---- shared vectors (tests/vectors.json)
func runVectors(_ path: String) {
    guard let data = FileManager.default.contents(atPath: path),
          let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
        failures += 1; print("FAIL could not read vectors at \(path)"); return
    }
    func str(_ v: Any?) -> String? { v is NSNull ? nil : v as? String }
    for c in root["requests"] as? [[String: Any]] ?? [] {
        let name = "vector[request] \(c["name"] ?? "?")"
        let r = RelayRequest(url: c["url"] as! String, referrer: str(c["referrer"]), label: str(c["label"]))
        let e = c["expect"] as? [String: Any] ?? [:]
        func kindName(_ k: RequestKind) -> String { k == .signIn ? "signIn" : k == .signOut ? "signOut" : "plainLink" }
        if let x = e["kind"] { check(kindName(r.kind) == x as? String, "\(name): kind") }
        if e.keys.contains("redirect") { check(r.redirect == str(e["redirect"]), "\(name): redirect") }
        if e.keys.contains("state") { check(r.state == str(e["state"]), "\(name): state") }
        if e.keys.contains("caller") { check(r.caller == str(e["caller"]), "\(name): caller") }
        if let x = e["appName"] { check(r.appName == x as? String, "\(name): appName") }
        if e.keys.contains("catchableScheme") { check(r.catchableScheme == str(e["catchableScheme"]), "\(name): catchableScheme") }
        if let x = e["lacksPKCE"] { check(r.lacksPKCE == (x as? Bool), "\(name): lacksPKCE") }
        if e.keys.contains("host") { check(r.host == str(e["host"]), "\(name): host") }
    }
    for c in root["accepts"] as? [[String: Any]] ?? [] {
        let name = "vector[accepts] \(c["name"] ?? "?")"
        let r = RelayRequest(url: c["url"] as! String, referrer: str(c["referrer"]))
        let got: String
        switch r.accepts(callback: c["callback"] as! String) { case .ok: got = "ok"; case .unrelated: got = "unrelated"; case .stateMismatch: got = "stateMismatch" }
        check(got == c["expect"] as? String, name)
    }
    for c in root["urlParams"] as? [[String: Any]] ?? [] {
        check(urlParams(c["url"] as! String) == (c["expect"] as? [String: String] ?? [:]), "vector[urlParams] \(c["name"] ?? "?")")
    }
    for c in root["shellQuote"] as? [[String]] ?? [] { check(shellQuote(c[0]) == c[1], "vector[shellQuote] \(c[0])") }
    for c in root["versionLess"] as? [[Any]] ?? [] {
        check(versionLess(c[0] as! String, c[1] as! String) == (c[2] as! Bool), "vector[versionLess] \(c[0]) < \(c[1])")
    }
}
runVectors(CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "../../tests/vectors.json")

print(failures == 0 ? "all tests passed" : "\(failures) FAILED")
exit(failures == 0 ? 0 : 1)
