// SPDX-License-Identifier: GPL-3.0-or-later
import Foundation

/// The connection to the Login Relay app on the phone, through `adb forward`.
///
/// adb accepts the TCP connection even when nothing is listening on the phone, then closes it.
/// So a connect only counts once it stays open for a moment.
final class PhoneLink {
    private var fd: Int32 = -1
    private let writeLock = NSLock()
    private var closed = false

    /// Called on the read thread for each JSON message from the phone.
    var onMessage: (([String: Any]) -> Void)?
    /// Called once, on the read thread, when an established link ends.
    var onClose: (() -> Void)?

    /// Connects to 127.0.0.1:port. Returns false if the phone app isn't listening right now.
    func open(port: UInt16) -> Bool {
        let s = socket(AF_INET, SOCK_STREAM, 0)
        guard s >= 0 else { return false }
        var one: Int32 = 1
        setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(s, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard rc == 0 else { Darwin.close(s); return false }

        // Give adb a moment to drop us if the phone side isn't there.
        var pfd = pollfd(fd: s, events: Int16(POLLIN), revents: 0)
        let ready = poll(&pfd, 1, 400)
        if ready > 0 {
            var peek: UInt8 = 0
            let n = recv(s, &peek, 1, MSG_PEEK)
            if n == 0 || n < 0 { Darwin.close(s); return false }  // closed straight away
        }
        fd = s
        closed = false
        let t = Thread { [weak self] in self?.readLoop() }
        t.name = "phone-link"
        t.start()
        return true
    }

    func send(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
        writeLock.lock()
        defer { writeLock.unlock() }
        guard fd >= 0, !closed else { return }
        var bytes = [UInt8](data)
        bytes.append(0x0A)
        var sent = 0
        while sent < bytes.count {
            let n = bytes.withUnsafeBytes { write(fd, $0.baseAddress! + sent, bytes.count - sent) }
            if n <= 0 { return }
            sent += n
        }
    }

    /// Hangs up; the read thread then ends and reports onClose.
    func close() {
        writeLock.lock()
        let s = fd
        closed = true
        writeLock.unlock()
        if s >= 0 { shutdown(s, SHUT_RDWR) }
    }

    private func readLoop() {
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = read(fd, &chunk, chunk.count)
            if n <= 0 { break }
            buffer.append(chunk, count: n)
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = buffer.subdata(in: buffer.startIndex..<nl)
                buffer.removeSubrange(buffer.startIndex...nl)
                if let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                    onMessage?(obj)
                }
            }
        }
        writeLock.lock()
        closed = true
        let s = fd
        fd = -1
        writeLock.unlock()
        if s >= 0 { Darwin.close(s) }
        onClose?()
    }
}
