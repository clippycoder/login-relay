// SPDX-License-Identifier: GPL-3.0-or-later
// Pure framing logic for the USB accessory link; no IOKit, so macos/Tests/run.sh covers it.
import Foundation

/// The bytes to write for one JSON message: the JSON, a newline, and, if that length is an exact
/// multiple of 64 (the full-speed bulk packet size; 512 and 1024 are multiples of it), one more
/// newline. A bulk transfer that ends on a packet boundary has no short packet to mark its end, so
/// the other side's read doesn't complete until more data arrives. The extra blank line is skipped
/// by the reader.
func usbFrame(_ json: Data) -> Data {
    var data = json
    data.append(0x0A)
    if data.count % 64 == 0 { data.append(0x0A) }
    return data
}

/// Collects bytes and hands back complete, non-empty lines.
struct LineSplitter {
    private var pending = Data()
    /// A line longer than this means the other side isn't speaking our protocol.
    static let maxLine = 1024 * 1024

    /// Returns the complete lines in `bytes` (plus anything held over), or nil if the stream is broken.
    mutating func feed(_ bytes: Data) -> [Data]? {
        var lines: [Data] = []
        for byte in bytes {
            if byte == 0x0A {
                if !pending.isEmpty { lines.append(pending); pending = Data() }
            } else {
                if pending.count >= LineSplitter.maxLine { return nil }
                pending.append(byte)
            }
        }
        return lines
    }
}
