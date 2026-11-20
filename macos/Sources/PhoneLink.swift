// SPDX-License-Identifier: GPL-3.0-or-later
// The link to the Login Relay app on the phone. Foundation only (no IOKit), so macos/Tests/run.sh
// covers it through a plain socket; the real phone supplies a USB accessory channel (Usb.swift).
import Foundation

/// What the engine needs from a connection to the phone.
protocol PhoneConnection: AnyObject {
    func send(_ object: [String: Any])
    func close()
}

/// A byte pipe to the phone: the USB accessory's bulk endpoints, or (test hook) a TCP socket.
protocol ByteChannel: AnyObject {
    /// USB bulk transfers need the extra blank line after a message that ends on a packet boundary
    /// (see `usbFrame`); a socket doesn't.
    var padsBulkPackets: Bool { get }
    /// Waits up to `timeoutMs` for bytes. Returns the count, 0 on timeout, or a negative number when the pipe is gone.
    func read(into buffer: UnsafeMutablePointer<UInt8>, capacity: Int, timeoutMs: Int) -> Int
    /// Writes everything or gives up. False means the message was dropped.
    func write(_ bytes: UnsafePointer<UInt8>, count: Int) -> Bool
    func close()
}

/// A TCP connection standing in for the USB pipe (the `LOGINRELAY_FAKE_PHONE` test hook).
final class SocketChannel: ByteChannel {
    private var fd: Int32
    let padsBulkPackets = false

    private init(fd: Int32) { self.fd = fd }

    /// Connects to "host:port". Nil if it can't.
    static func connect(_ hostPort: String) -> SocketChannel? {
        guard let colon = hostPort.lastIndex(of: ":") else { return nil }
        let host = String(hostPort[..<colon]), port = String(hostPort[hostPort.index(after: colon)...])
        var hints = addrinfo(ai_flags: 0, ai_family: AF_UNSPEC, ai_socktype: SOCK_STREAM, ai_protocol: 0,
                             ai_addrlen: 0, ai_canonname: nil, ai_addr: nil, ai_next: nil)
        var list: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host.isEmpty ? "127.0.0.1" : host, port, &hints, &list) == 0, let first = list else { return nil }
        defer { freeaddrinfo(first) }
        var cursor: UnsafeMutablePointer<addrinfo>? = first
        while let ai = cursor {
            let s = socket(ai.pointee.ai_family, ai.pointee.ai_socktype, ai.pointee.ai_protocol)
            if s >= 0 {
                var one: Int32 = 1
                setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
                if Darwin.connect(s, ai.pointee.ai_addr, ai.pointee.ai_addrlen) == 0 { return SocketChannel(fd: s) }
                Darwin.close(s)
            }
            cursor = ai.pointee.ai_next
        }
        return nil
    }

    func read(into buffer: UnsafeMutablePointer<UInt8>, capacity: Int, timeoutMs: Int) -> Int {
        var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
        let ready = poll(&pfd, 1, Int32(timeoutMs))
        if ready == 0 { return 0 }
        if ready < 0 { return errno == EINTR ? 0 : -1 }
        let n = Darwin.read(fd, buffer, capacity)
        return n > 0 ? n : -1   // 0 is end-of-file: the peer hung up
    }

    func write(_ bytes: UnsafePointer<UInt8>, count: Int) -> Bool {
        var sent = 0
        while sent < count {
            let n = Darwin.write(fd, bytes + sent, count - sent)
            if n <= 0 { return false }
            sent += n
        }
        return true
    }

    func close() {
        if fd >= 0 { Darwin.close(fd); fd = -1 }
    }
}

/// The connection to the Login Relay app on the phone: line-delimited JSON over a `ByteChannel`.
/// Bulk USB has no "connection closed", so the link pings the phone every 2 s and tracks whether
/// anything has come back recently ("alive").
final class PhoneLink: PhoneConnection {
    var onMessage: (([String: Any]) -> Void)?
    var onClose: (() -> Void)?
    /// Called on the read thread when the phone app starts or stops answering.
    var onAlive: ((Bool) -> Void)?

    /// No message for this long means the phone app isn't answering.
    static let silence: TimeInterval = 8
    private let channel: ByteChannel
    private let writeLock = NSLock()
    private var closed = false          // guarded by writeLock
    private let stopLock = NSLock()
    private var stopping = false        // guarded by stopLock

    init(channel: ByteChannel) { self.channel = channel }

    func start() {
        let t = Thread { [weak self] in self?.readLoop() }
        t.name = "phone-link"
        t.start()
    }

    func send(_ object: [String: Any]) {
        guard let json = try? JSONSerialization.data(withJSONObject: object) else { return }
        let frame = channel.padsBulkPackets ? usbFrame(json) : json + Data([0x0A])
        writeLock.lock()
        defer { writeLock.unlock() }
        if closed { return }
        frame.withUnsafeBytes { raw in
            guard let base = raw.bindMemory(to: UInt8.self).baseAddress else { return }
            var offset = 0
            while offset < frame.count {
                let chunk = min(frame.count - offset, 16384)
                // A failure or timeout means the phone app isn't reading; drop the message. Liveness covers the rest.
                if !channel.write(base + offset, count: chunk) { return }
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
            channel.close()
            writeLock.unlock()
            onClose?()
        }
        while !isStopping {
            let n = channel.read(into: &buffer, capacity: buffer.count, timeoutMs: 200)
            if n < 0 { return }
            if n > 0 {
                lastHeard = Date()
                guard let lines = splitter.feed(Data(buffer[0..<n])) else { return }
                for line in lines {
                    if let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] { onMessage?(obj) }
                }
            }
            if Date().timeIntervalSince(lastPing) > 2 {
                send(["type": "ping"])
                lastPing = Date()
            }
            let nowAlive = Date().timeIntervalSince(lastHeard) < PhoneLink.silence
            if nowAlive != alive {
                alive = nowAlive
                onAlive?(alive)
            }
        }
    }
}
