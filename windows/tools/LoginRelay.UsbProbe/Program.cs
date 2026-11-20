// SPDX-License-Identifier: GPL-3.0-or-later
// Command-line test rig for Login Relay's Windows USB accessory layer (port of macos/Tools/aoa_probe.c).
// Not part of the app: run it on a real PC with a phone to check everything before trusting the full app.
//
//   LoginRelay.UsbProbe list                 what libusb sees AND what Windows sees, and which setup step applies
//   LoginRelay.UsbProbe run [location]       switch a phone into accessory mode, open it, and talk to Login Relay:
//                                            prints every line the phone sends, pings it every 2 s, and sends what
//                                            you type: "callback <url>", "done", or any raw JSON line.
//
// libusb-1.0.dll must sit next to this program. Windows can only give libusb a device that is bound to the
// WinUSB driver; `list` says which devices are not, and `run` says which Zadig step is next.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using LoginRelay.App;
using LoginRelay.Core;

namespace LoginRelay.UsbProbe
{
    internal static class Program
    {
        private static readonly Stopwatch Clock = Stopwatch.StartNew();

        private static void Log(string text)
        {
            Console.WriteLine("[" + Clock.Elapsed.TotalSeconds.ToString("0.00").PadLeft(6) + "s] " + text);
            Console.Out.Flush();
        }

        private static int Main(string[] args)
        {
            try
            {
                Console.OutputEncoding = new UTF8Encoding(false);
            }
            catch (Exception)
            {
            }
            if (args.Length > 0 && args[0] == "list")
            {
                return ListCommand();
            }
            if (args.Length > 0 && args[0] == "run")
            {
                return RunCommand(args.Length > 1 ? args[1] : null);
            }
            Console.Error.WriteLine("usage: LoginRelay.UsbProbe list | run [location]");
            Console.Error.WriteLine("  location is the first column of `list` (for example 1-4.2).");
            return 2;
        }

        // ---- shared helpers ---------------------------------------------------------------------------

        private static string Describe(UsbException ex)
        {
            string libusb = ex.LibusbCode != 0 ? " [" + UsbText.LibusbErrorName(ex.LibusbCode) + "]" : "";
            return "code " + ex.Code + libusb + ": " + (ex.Detail.Length > 0 ? ex.Detail : ex.Message);
        }

        private static IReadOnlyList<UsbDevice>? TryList(IUsbBackend backend)
        {
            try
            {
                return backend.ListDevices();
            }
            catch (UsbException ex)
            {
                Log("libusb: couldn't list devices (" + Describe(ex) + ")");
                return null;
            }
        }

        private static IReadOnlyList<PnpDevice> TryWindows(bool announce)
        {
            try
            {
                return new WmiUsbDiagnostics().QueryUsbDevices();
            }
            catch (Exception ex)
            {
                if (announce)
                {
                    Log("Windows (WMI): the query failed: " + ex.Message);
                }
                return new List<PnpDevice>();
            }
        }

        private static void PrintState(IReadOnlyList<UsbDevice> libusb, IReadOnlyList<PnpDevice> windows)
        {
            UsbSetupInfo info = UsbDiagnosisLogic.Diagnose(libusb, windows);
            Console.WriteLine();
            Console.WriteLine("Setup state: " + info.State + (info.HasId ? "  (USB ID " + info.UsbId + ", " + info.DeviceName + ")" : ""));
            Console.WriteLine(UsbSetupText.Title(info));
            Console.WriteLine(UsbSetupText.Steps(info));
            Console.Out.Flush();
        }

        private static void PrintDiagnosisNow(IReadOnlyList<UsbDevice> libusb)
        {
            PrintState(libusb, TryWindows(true));
        }

        // ---- list -------------------------------------------------------------------------------------

        private static int ListCommand()
        {
            IReadOnlyList<UsbDevice>? libusb;
            using (var backend = new LibUsbBackend())
            {
                libusb = TryList(backend);
            }
            Console.WriteLine("== libusb's view (devices Windows has bound to WinUSB can be opened; the others are listed but not usable) ==");
            if (libusb == null)
            {
                Console.WriteLine("(libusb is not available: is libusb-1.0.dll next to this program?)");
                libusb = new List<UsbDevice>();
            }
            Console.WriteLine("{0,-10} {1,-9} {2,-8} {3,-4} {4,-9} {5}", "location", "vid:pid", "android?", "aoa", "openable", "name / serial");
            foreach (UsbDevice d in libusb)
            {
                string name = d.LooksAndroid ? d.Name + (d.Serial.Length > 0 ? " / " + d.Serial : "") : "";
                string openable = d.LooksAndroid ? (d.Openable ? "yes" : "NO") : "-";
                Console.WriteLine(
                    "{0,-10} {1:x4}:{2:x4} {3,-8} {4,-4} {5,-9} {6}",
                    d.Location, d.VendorId, d.ProductId, d.LooksAndroid ? "likely" : "-", d.IsAccessory ? "yes" : "-", openable, name);
            }

            Console.WriteLine();
            Console.WriteLine("== Windows' view (WMI Win32_PnPEntity, USB\\VID_ only) ==");
            IReadOnlyList<PnpDevice> windows = TryWindows(true);
            foreach (PnpDevice p in windows.OrderBy(x => x.DeviceId, StringComparer.OrdinalIgnoreCase))
            {
                ushort vid, pid;
                int mi;
                string ids = UsbDiagnosisLogic.TryParseDeviceId(p.DeviceId, out vid, out pid, out mi)
                    ? vid.ToString("x4") + ":" + pid.ToString("x4") + (mi >= 0 ? " MI_" + mi.ToString("x2") : "")
                    : "?";
                bool phoneLike = p.CompatibleIds.Any(UsbDiagnosisLogic.IsPhoneCompatibleId);
                Console.WriteLine(
                    "{0,-14} service={1,-12} error={2,-3} present={3,-5} {4}{5}",
                    ids, p.Service ?? "-", p.ConfigManagerErrorCode, p.Present.HasValue ? p.Present.Value.ToString() : "?", p.Name,
                    phoneLike ? "   <- looks like a phone" : "");
                if (phoneLike || Aoa.IsAccessoryIds(vid, pid))
                {
                    Console.WriteLine("               " + p.DeviceId);
                    Console.WriteLine("               compatible IDs: " + string.Join(", ", p.CompatibleIds));
                }
            }
            if (windows.Count == 0)
            {
                Console.WriteLine("(nothing)");
            }

            PrintState(libusb, windows);
            return 0;
        }

        // ---- run --------------------------------------------------------------------------------------

        private static UsbDevice? FindAccessory(IReadOnlyList<UsbDevice> list, string location)
        {
            UsbDevice? same = list.FirstOrDefault(d => d.IsAccessory && d.Location == location);
            if (same != null)
            {
                return same;
            }
            List<UsbDevice> accessories = list.Where(d => d.IsAccessory).ToList();
            return accessories.Count == 1 ? accessories[0] : null;
        }

        private static void SendLine(IUsbConnection conn, string json)
        {
            byte[] frame = UsbFraming.Frame(Encoding.UTF8.GetBytes(json));
            int offset = 0;
            while (offset < frame.Length)
            {
                int chunk = Math.Min(frame.Length - offset, UsbFraming.WriteChunkSize);
                int written;
                try
                {
                    written = conn.Write(frame, offset, chunk, 1000);
                }
                catch (UsbException ex)
                {
                    Log("write failed (" + Describe(ex) + ")");
                    return;
                }
                if (written <= 0)
                {
                    Log("write timed out: the phone isn't reading");
                    return;
                }
                offset += written;
            }
        }

        private static int RunCommand(string? wantedLocation)
        {
            using (var backend = new LibUsbBackend())
            {
                IReadOnlyList<UsbDevice>? all = TryList(backend);
                if (all == null)
                {
                    return 1;
                }
                List<UsbDevice> matches = wantedLocation != null
                    ? all.Where(d => d.Location == wantedLocation).ToList()
                    : all.Where(d => d.LooksAndroid).ToList();
                if (matches.Count == 0)
                {
                    Log("No candidate phone. Plug it in and pick “File transfer” in its USB notification, or run `list`.");
                    PrintDiagnosisNow(all);
                    return 1;
                }
                if (matches.Count > 1)
                {
                    // An accessory-mode device is the one to talk to.
                    UsbDevice? acc = matches.FirstOrDefault(d => d.IsAccessory);
                    if (acc != null && wantedLocation == null)
                    {
                        matches = new List<UsbDevice> { acc };
                    }
                    else
                    {
                        Log(matches.Count + " candidates; give a location from `list`:");
                        foreach (UsbDevice d in matches)
                        {
                            Log("  " + d.Location + "  " + d.VendorId.ToString("x4") + ":" + d.ProductId.ToString("x4") + "  " + d.Name);
                        }
                        return 1;
                    }
                }
                UsbDevice phone = matches[0];
                Log("Phone: " + phone.Name + "  " + phone.VendorId.ToString("x4") + ":" + phone.ProductId.ToString("x4") + " at " + phone.Location
                    + (phone.IsAccessory ? " (already in accessory mode)" : ""));
                if (!phone.Openable)
                {
                    Log("libusb lists it but can't open it: Windows hasn't bound it to WinUSB.");
                    PrintDiagnosisNow(all);
                    return 1;
                }

                string location = phone.Location;
                if (!phone.IsAccessory)
                {
                    Stopwatch s = Stopwatch.StartNew();
                    UsbSwitchResult r = backend.SwitchToAccessory(location, Environment.MachineName);
                    Log("switch -> " + r.Code + " (AOA protocol " + r.ProtocolVersion + ") in " + s.Elapsed.TotalSeconds.ToString("0.00") + "s"
                        + (r.Detail.Length > 0 ? "  [" + r.Detail + "]" : ""));
                    if (r.Code != UsbCode.Ok)
                    {
                        Log(UsbText.ErrorText(r, phone.Name));
                        if (r.Code == UsbCode.NoDriver)
                        {
                            PrintDiagnosisNow(all);
                        }
                        return 1;
                    }

                    bool appeared = false;
                    IReadOnlyList<UsbDevice> after = all;
                    for (int i = 0; i < 60; i++)    // up to 12 s: Android gives up after 10
                    {
                        Thread.Sleep(200);
                        after = TryList(backend) ?? after;
                        UsbDevice? acc = FindAccessory(after, location);
                        if (acc != null)
                        {
                            Log("accessory device appeared: " + acc.VendorId.ToString("x4") + ":" + acc.ProductId.ToString("x4")
                                + " at " + acc.Location + " after " + s.Elapsed.TotalSeconds.ToString("0.00") + "s"
                                + (acc.Openable ? "" : "  (but libusb can't open it)"));
                            if (!acc.Openable)
                            {
                                PrintDiagnosisNow(after);
                                return 1;
                            }
                            location = acc.Location;
                            appeared = true;
                            break;
                        }
                    }
                    if (!appeared)
                    {
                        Log("the phone never reappeared as an accessory that libusb can see");
                        PrintDiagnosisNow(after);
                        return 1;
                    }
                }

                IUsbConnection? conn = null;
                for (int attempt = 1; attempt <= 6 && conn == null; attempt++)
                {
                    try
                    {
                        conn = backend.OpenAccessory(location);
                    }
                    catch (UsbException ex)
                    {
                        Log("open attempt " + attempt + " failed (" + Describe(ex) + ")");
                        if (ex.Code == UsbCode.NoDriver)
                        {
                            PrintDiagnosisNow(TryList(backend) ?? all);
                            return 1;
                        }
                        Thread.Sleep(500);
                    }
                }
                if (conn == null)
                {
                    return 1;
                }
                try
                {
                    Talk(conn);
                }
                finally
                {
                    conn.Close();
                }
            }
            return 0;
        }

        private static void Talk(IUsbConnection conn)
        {
            Log("opened. Waiting for Login Relay on the phone (it launches by itself; the first time Android asks to allow it).");
            Log("type: callback <url> | done | <raw json>");

            var typed = new ConcurrentQueue<string>();
            var reader = new Thread(() =>
            {
                try
                {
                    string? line;
                    while ((line = Console.In.ReadLine()) != null)
                    {
                        typed.Enqueue(line);
                    }
                }
                catch (Exception)
                {
                }
            });
            reader.IsBackground = true;
            reader.Start();

            byte[] buffer = new byte[UsbFraming.ReadBufferSize];
            var splitter = new LineSplitter();
            double lastPing = double.NegativeInfinity;
            double lastHeard = double.NegativeInfinity;
            bool silentNoted = false;
            while (true)
            {
                int n;
                try
                {
                    n = conn.Read(buffer, 200);
                }
                catch (UsbException ex)
                {
                    Log("read error (" + Describe(ex) + "): the phone was unplugged or the link broke");
                    return;
                }
                if (n > 0)
                {
                    lastHeard = Clock.Elapsed.TotalSeconds;
                    if (silentNoted)
                    {
                        silentNoted = false;
                        Log("(the phone is answering again)");
                    }
                    List<byte[]>? lines = splitter.Feed(buffer, n);
                    if (lines == null)
                    {
                        Log("a line over 1 MiB: the stream is broken");
                        return;
                    }
                    foreach (byte[] line in lines)
                    {
                        Log("phone: " + Encoding.UTF8.GetString(line));
                    }
                }
                double now = Clock.Elapsed.TotalSeconds;
                if (now - lastPing > 2)
                {
                    SendLine(conn, "{\"type\":\"ping\"}");
                    lastPing = Clock.Elapsed.TotalSeconds;
                }
                if (!silentNoted && lastHeard > double.NegativeInfinity && now - lastHeard > 8)
                {
                    silentNoted = true;
                    Log("(nothing from the phone for 8 s: Login Relay there is idle or was reclaimed by Android; a sign-in starts it again)");
                }
                string? input;
                while (typed.TryDequeue(out input))
                {
                    string text = input.TrimEnd('\r', '\n');
                    if (text.StartsWith("callback ", StringComparison.Ordinal))
                    {
                        var fields = new Dictionary<string, string>();
                        fields["type"] = "callback";
                        fields["url"] = text.Substring("callback ".Length);
                        SendLine(conn, JsonSerializer.Serialize(fields));
                    }
                    else if (text == "done")
                    {
                        SendLine(conn, "{\"type\":\"done\"}");
                    }
                    else if (text.Length > 0)
                    {
                        SendLine(conn, text);
                    }
                }
            }
        }
    }
}
