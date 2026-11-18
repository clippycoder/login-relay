// SPDX-License-Identifier: GPL-3.0-or-later
import CryptoKit
import Foundation

/// Downloads Google's Android platform-tools (which contains adb) for people who don't have it.
/// It comes straight from dl.google.com and is checked against the SHA-1 in Google's own
/// repository manifest. Nothing is redistributed by this project.
enum AdbInstaller {
    static let manifestURL = URL(string: "https://dl.google.com/android/repository/repository2-3.xml")!
    static let archiveBase = "https://dl.google.com/android/repository/"
    static let licenseURL = URL(string: "https://developer.android.com/studio/terms")!

    static var installDir: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Login Relay", isDirectory: true)
    }
    static var installedPath: String { installDir.appendingPathComponent("platform-tools/adb").path }

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Blocking; call off the main thread.
    static func install(progress: (String) -> Void) throws {
        progress("Reading Google's download list…")
        let manifest = String(decoding: try fetch(manifestURL, timeout: 60), as: UTF8.self)
        let (sha1, file) = try macArchive(inManifest: manifest)

        progress("Downloading platform-tools…")
        let zip = try fetch(URL(string: archiveBase + file)!, timeout: 300)
        let digest = Insecure.SHA1.hash(data: zip).map { String(format: "%02x", $0) }.joined()
        guard digest == sha1 else {
            throw Failure(message: "The download didn't match Google's checksum, so it was discarded.")
        }

        progress("Unpacking…")
        let fm = FileManager.default
        let staging = fm.temporaryDirectory.appendingPathComponent("loginrelay-pt-\(UUID().uuidString)")
        try fm.createDirectory(at: staging, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: staging) }
        let zipFile = staging.appendingPathComponent("pt.zip")
        try zip.write(to: zipFile)
        let unpack = Adb.exec("/usr/bin/ditto", ["-x", "-k", zipFile.path, staging.path], timeout: 120)
        guard unpack.ok else { throw Failure(message: "Couldn't unpack the download: \(unpack.err)") }

        let fresh = staging.appendingPathComponent("platform-tools")
        guard fm.isExecutableFile(atPath: fresh.appendingPathComponent("adb").path) else {
            throw Failure(message: "The download didn't contain adb.")
        }
        try fm.createDirectory(at: installDir, withIntermediateDirectories: true)
        let target = installDir.appendingPathComponent("platform-tools")
        if fm.fileExists(atPath: target.path) { try fm.removeItem(at: target) }
        try fm.moveItem(at: fresh, to: target)
        progress("Installed.")
    }

    /// Pulls the macOS archive's checksum and file name out of the platform-tools entry.
    static func macArchive(inManifest xml: String) throws -> (sha1: String, file: String) {
        guard let start = xml.range(of: "<remotePackage path=\"platform-tools\""),
              let end = xml.range(of: "</remotePackage>", range: start.upperBound..<xml.endIndex)
        else { throw Failure(message: "Google's download list has no platform-tools entry.") }
        let block = String(xml[start.lowerBound..<end.lowerBound])
        let pattern = #"<checksum type="sha1">([0-9a-f]{40})</checksum>\s*<url>([^<]*darwin[^<]*)</url>"#
        guard let re = try? NSRegularExpression(pattern: pattern),
              let m = re.firstMatch(in: block, range: NSRange(block.startIndex..., in: block)),
              let r1 = Range(m.range(at: 1), in: block), let r2 = Range(m.range(at: 2), in: block)
        else { throw Failure(message: "Google's download list has no macOS platform-tools.") }
        return (String(block[r1]), String(block[r2]))
    }

    private static func fetch(_ url: URL, timeout: TimeInterval) throws -> Data {
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        let sem = DispatchSemaphore(value: 0)
        var result: Result<Data, Error> = .failure(Failure(message: "No response."))
        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error { result = .failure(error) }
            else if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                result = .failure(Failure(message: "\(url.host ?? "server") answered \(http.statusCode)."))
            } else if let data { result = .success(data) }
            sem.signal()
        }.resume()
        sem.wait()
        return try result.get()
    }
}
