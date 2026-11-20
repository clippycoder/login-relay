// SPDX-License-Identifier: GPL-3.0-or-later
// Pure framing logic for the USB accessory link (port of macos/Sources/UsbFraming.swift). No USB library
// here, so the unit tests cover it everywhere.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;

namespace LoginRelay.Core
{
    public static class UsbFraming
    {
        /// <summary>Reads must use a buffer of at least this many bytes (a short buffer drops the rest of a transfer).</summary>
        public const int ReadBufferSize = 16384;

        /// <summary>Writes are split into pieces of at most this many bytes.</summary>
        public const int WriteChunkSize = 16384;

        /// <summary>The full-speed bulk packet size. 512 and 1024 are multiples of it.</summary>
        public const int PacketSize = 64;

        /// <summary>A line longer than this means the other side isn't speaking our protocol.</summary>
        public const int MaxLine = 1024 * 1024;

        /// <summary>
        /// The bytes to write for one JSON message: the JSON, a newline and, if that length is an exact
        /// multiple of 64, one more newline. A bulk transfer that ends on a packet boundary has no short
        /// packet to mark its end, so the other side's read doesn't complete until more data arrives. The
        /// extra blank line is skipped by the reader.
        /// </summary>
        public static byte[] Frame(byte[] json)
        {
            bool extra = (json.Length + 1) % PacketSize == 0;
            byte[] data = new byte[json.Length + (extra ? 2 : 1)];
            Buffer.BlockCopy(json, 0, data, 0, json.Length);
            data[json.Length] = 0x0A;
            if (extra)
            {
                data[json.Length + 1] = 0x0A;
            }
            return data;
        }

        /// <summary>Parses one line as a JSON object (a detached copy), or returns null if it isn't one.</summary>
        public static JsonElement? ParseMessage(byte[] line)
        {
            if (line.Length == 0)
            {
                return null;
            }
            try
            {
                using (JsonDocument doc = JsonDocument.Parse(line))
                {
                    if (doc.RootElement.ValueKind != JsonValueKind.Object)
                    {
                        return null;
                    }
                    return doc.RootElement.Clone();
                }
            }
            catch (JsonException)
            {
                return null;
            }
        }
    }

    /// <summary>Collects bytes and hands back complete, non-empty lines.</summary>
    public sealed class LineSplitter
    {
        private readonly MemoryStream _pending = new MemoryStream();

        /// <summary>
        /// Returns the complete lines in the first <paramref name="count"/> bytes of <paramref name="bytes"/>
        /// (plus anything held over), or null if the stream is broken (a line that is too long).
        /// </summary>
        public List<byte[]>? Feed(byte[] bytes, int count)
        {
            var lines = new List<byte[]>();
            for (int i = 0; i < count; i++)
            {
                byte b = bytes[i];
                if (b == 0x0A)
                {
                    if (_pending.Length > 0)
                    {
                        lines.Add(_pending.ToArray());
                        _pending.SetLength(0);
                    }
                }
                else
                {
                    if (_pending.Length >= UsbFraming.MaxLine)
                    {
                        return null;
                    }
                    _pending.WriteByte(b);
                }
            }
            return lines;
        }
    }
}
