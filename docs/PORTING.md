# Porting the desktop app

The macOS app (`macos/Sources/*.swift`) is the reference implementation. A port on another system
must behave the same way; the strings and timings below are the contract. When this file and the
Swift code disagree, the Swift code wins (and this file needs fixing).

Desktop apps (and the phone app) talk **only over USB, in Android's USB accessory mode**. There is no adb
support; the older adb transport lives only in the v0.4.0 release.

Three things are shared on purpose:

- `tests/vectors.json`: how sign-in pages are classified and how return addresses are matched.
  Every implementation must pass all of it.
- `tests/e2e/`: a fake phone (`fake_phone.py`, the USB-mode protocol over a plain socket) plus a driver
  (`run_e2e.py`) that tests a whole app without a phone.
- The phone protocol, below.

## What the app does

A tray / menu bar app that, when an Android app on a USB-connected phone asks to open a web page
(Login Relay on the phone forwards it), opens the page in the computer's browser, catches the
redirect back to the app, and sends it to the phone, which gives it to the app.

```
Login Relay (phone) <------ USB accessory (bulk IN/OUT) ------> the app
   {"type":"open",url,referrer,label}  ------------------------------>  open url in the browser
                                                                         claim the return scheme
                                                                         wait for the redirect
   {"type":"callback",url}  <------------------------------------------  callback arrives
   {"type":"delivered"}  ---------------------------------------------->  (phone vetted it and started the app)
   {"type":"done"}  <-------------------------------------------------  (after 1.5 s)
```

## Phone protocol

Newline-delimited JSON over the USB link (framing below).

| direction | message |
|---|---|
| phone → app | `{"v":1,"type":"hello","transport":"usb","version":"<app version>"}` on every new link |
| phone → app | `{"v":1,"type":"open","url":…,"referrer":"android-app://<package>","label":"<app name>"}`; `referrer` and `label` may be `null` or missing |
| phone → app | `{"type":"cancel"}`, `{"type":"pong"}`, `{"type":"delivered"}`, `{"type":"error","text":…}` |
| app → phone | `{"type":"status","text":…}`, `{"type":"error","text":…}`, `{"type":"done"}` (closes the phone's screen), `{"type":"ping"}`, `{"type":"callback","url":…}` |

Unknown messages are ignored. The phone resends its pending `open` to every new connection until it
gets `done`, so a reconnect legitimately starts the same login again.

## Pure logic (port this first; test it with the vectors)

`Redirect.swift` has everything: `urlParams`, `sameRedirect`, `RelayRequest` (kind, redirect, state,
caller, label, appName, catchableScheme, lacksPKCE, host, `accepts(callback:)`).

- Sign-in uses `redirect_uri`; sign-out uses `post_logout_redirect_uri` or Auth0's `returnTo`; anything
  else is a plain link.
- `accepts`: `unrelated` unless the callback equals the redirect address (scheme case-insensitive,
  query and fragment ignored, trailing slashes ignored); then `stateMismatch` if the request has a
  `state` and the callback's differs or is missing; else `ok`.

## Engine

State lives on the UI thread; one background thread watches the USB bus. See `RelayEngine.swift`.

**Handles.** Close every handle, process and socket you open: the first macOS build leaked two file
descriptors a second and silently died after two minutes. Verify by watching the process's handle count
while idle.

**Watching the bus** (background thread, ticks every second; "Check Again" forces a pass): list USB devices
and pick the candidate phone (below); if none, the phase is *no phone*. If the phone isn't in accessory mode,
switch it (below), wait for it to reappear, open the accessory interface and link (ping/pong, below). When the
link ends or the phone leaves the bus, look again immediately. A watchdog on the UI thread: if the loop has
made no progress for 30 s, log it, close the link, and start a new loop (abandon the old one). The progress
marker must be written by the loop thread itself, so a modal dialog on the UI thread can't trip the watchdog.

**Phases** (derived, not stored): no phone → connecting (a phone is plugged in; detail says what we're waiting
for, and while the link isn't alive yet the headline is "Connecting to <phone>…") → ready → working (a login is
in progress). A phone that is present but can't be opened gets a plain "Can't open the phone" status
(Windows: no WinUSB driver). The detail says a WinUSB driver is probably missing, names Zadig and links the
README's "Windows drivers" section (https://github.com/clippycoder/login-relay#windows-drivers); a "Driver
Help…" menu item opens that link. There is no guided repair.

**One login**, on the UI thread, when `open` arrives:

1. Classify with `RelayRequest`. Log `"<appName> wants to open a <verb> page at <host>."`
2. Plain link: open it in the browser, send `status` "Opened on your computer.", send `done` after 1 s.
3. Authorization-code sign-in without PKCE (`lacksPKCE`): ask first ("<app> is signing in without
   PKCE"; Cancel / Continue Anyway). If declined, send `error` "Cancelled on the computer." and stop.
4. Claim the return scheme (`catchableScheme`) for this login (below). If the claim fails or the
   redirect is an ordinary web address, fall back to asking the user to paste the final address
   (a dialog; keep asking until it matches or the user cancels; `stateMismatch` and `unrelated` get
   different messages).
5. Send `status` "Finish signing in on your computer." (sign-out: "Finishing sign-out on your computer.").
6. Open the page in the default browser, unless `LOGINRELAY_BROWSER=none`. Log "Opened the page in
   your browser. If it asks to open Login Relay, allow it." Optionally notify.
7. Start a 15-minute timeout → send `error` "Timed out waiting for the browser.", cancel the login.
8. When a URL reaches the app (from the OS, or pasted): `accepts` → `ok`: release the claim, log
   "Returning to <app> on the phone…", and send `callback` (see "Delivery" below).
   `stateMismatch`: log "Ignored a return address from a different sign-in (state mismatch)." and keep
   waiting. `unrelated`: ignore silently.
9. Cancel (from the phone's `cancel` message, the link closing, the phone vanishing, the menu): release
   the claim, cancel the timeout, log the reason; if the cancel came from the computer, send `done` so
   the phone's screen closes. A new `open` replaces a login in progress.

**Claiming a return scheme.** Make this app the OS handler for the scheme for the length of one
login, receive the URLs, then put the previous handler back. The claim must be:

- **recorded first** (persist `scheme → previous handler or ""`), and **cleaned up on the next
  launch** if the app crashed mid-login;
- **verified**: if the OS refuses, treat it as "no claim" and use the paste fallback;
- **restored exactly**: previous handler back if there was one, otherwise cleared.

macOS: `LSSetDefaultHandlerForURLScheme(scheme, ownBundleId)`; clear with an empty string; URLs
arrive as Apple events. Windows: `HKCU\Software\Classes\<scheme>` with `URL Protocol`, `shell\open\command`
= `"<exe>" --url "%1"`; back up and restore the previous key tree. Linux: a `.desktop` file with
`MimeType=x-scheme-handler/<scheme>;` and `Exec=… --url %u` plus the default-handler setting; restore or
reset afterwards.

Where the OS starts a *second process* for each URL (Windows, Linux), the second process must hand its
`--url` to the first through single-instance IPC and exit.

**The phone app is not managed from the desktop.** The desktop apps may bundle `login-relay.apk` so the user
can find it, but nothing installs or updates it.

## USB accessory mode

USB accessory mode (Android Open Accessory, "AOA", the protocol Android Auto uses) needs no Developer options
and no USB debugging, which filtered or managed phones often can't or shouldn't have.
The macOS app has it (`Usb.swift`, `UsbFraming.swift`, `aoa_mac.c`, and `usbLoop` in `RelayEngine.swift`),
and so does the Linux app (`usbaoa.py`, `usblink.py`, `usbframing.py`, over pyusb). The Windows app has it
too (`LibUsb.cs`, `Usb.cs`, `UsbLink.cs`, `RelayEngine.Usb.cs`, over a bundled libusb 1.0.30;
`windows/tools/LoginRelay.UsbProbe` is a console tool for trying it on a real PC) but needs a driver: WinUSB,
bound to the phone and then to the accessory interface. libusb can only open devices bound to WinUSB, and the
stock Windows drivers for a phone expose no way to send the switch. The app does not install or guide the
install of drivers; it shows a plain "Can't open the phone" status and the README documents the driver
(Zadig) steps.

The phone app (0.4.0+) registers for `USB_ACCESSORY_ATTACHED` with a filter for manufacturer
"Login Relay" and model "Desktop", needs no permission, and Android asks the user once.

**Switching** (all control transfers on endpoint 0 of the phone):

| step | bmRequestType | bRequest | other |
|---|---|---|---|
| get protocol | 0xC0 | 51 | 2 bytes in, little-endian; must be >= 1 |
| send string | 0x40 | 52 | wIndex 0..5, data = the string + NUL |
| start | 0x40 | 53 | the phone then drops off the bus; an error here is expected |

Strings: 0 manufacturer `Login Relay`, 1 model `Desktop` (these two must match the phone's filter exactly),
2 description = the computer's name (shown on the phone), 3 version `1`, 4 URI
`https://github.com/clippycoder/login-relay`, 5 serial = the location id.

The phone reappears as Google VID `0x18D1`, PID `0x2D00..0x2D05` (`2D01` = accessory + a debugging interface) at the same
bus location. Android gives up if the host hasn't reconnected in 10 s, so poll for ~12 s. Interface 0 has one
bulk IN and one bulk OUT endpoint; claim only that one (with `2D01` interface 1 is the debugging interface). Open the interface
**directly, not the whole device**: another program (such as a debugging tool's server) can hold a phone
exclusively (macOS reports `kIOReturnExclusiveAccess`, 0xe00002c5, on the *switch*). Don't try to stop that
program: report "Another program has the phone open. Close it, then choose Check Again." and keep retrying.

**Candidates.** Only ask a device about AOA if it exposes an interface with class/subclass/protocol
`06/01/*` (PTP/MTP), `FF/42/01` (a phone's debugging interface) or `FF/FF/00` (vendor MTP), or is already in accessory mode. Never
probe anything else: an unrelated Qualcomm download-mode device was on the bus during testing and must be
left alone. One candidate is used automatically; with several the user picks (`usbSelected`). Rate-limit
switch attempts (one per 20 s per location; forget a location when it leaves the bus).

**Framing.** Line-delimited JSON. Reads use a buffer of at least 16384 bytes (a partial read
drops the rest of the transfer); writes are chunked to at most 16384. A bulk transfer whose length is an
exact multiple of the packet size ends without a short packet and the other side's read never completes, so
after the `\n` append **one more `\n` if the length is a multiple of 64** (512 and 1024 are multiples of it);
readers skip blank lines. Lines over 1 MiB mean the stream is broken. See `UsbFraming.swift`.

**Liveness.** Bulk USB has no "connection closed". The computer sends `{"type":"ping"}` every 2 s; the link
is *alive* if any message arrived in the last 8 s. Phase is `connecting(device, detail)` until alive, then
`ready`; if it stops being alive mid-login, cancel the login. The phone's USB link outlives its screen (the
accessory file can't be reopened while a read is blocked in the kernel) and answers pings itself, so an alive
link means the phone app's process is running, not that a sign-in is on screen.

**Delivery.** The computer sends `callback`. The phone
checks it against the page it opened (same redirect address and, if present, same `state`; the rules in
`android/.../Redirect.java`, tested against `tests/vectors.json`), then calls `startActivity(VIEW, url)` with
`setPackage(<caller>)` from its foreground screen. It answers `delivered`, or `error` with a reason
("That result is from a different sign-in.", "That isn't this sign-in's return address.", "The app didn't
accept the result."). The computer logs `Done: <app> is finishing the <verb>.` on `delivered`, logs
`The phone says: <text>` on `error`, and logs `The phone didn't confirm it handed the result to the app.` if
neither arrives within 10 s. The phone closes its screen itself after delivering.

**Verified** on a CAT S22 Flip (Android 11) with USB debugging off and the phone in "File transfer" mode: it
enumerates by itself (`18D1:4EE1`), the switch works, and it comes back as `18D1:2D00`. **Not verified:** a phone
in "No data transfer" / "Charging only" mode (the test phone switches itself back to File transfer), which may not enumerate until the user picks "File transfer" in its USB
notification (the apps say so in the "no phone found" hint). An MDM that disables USB data (for example
`setUsbDataSignalingEnabled(false)`) will block this mode.

## Interface

- **Tray / menu bar icon**: three states: ready, off (no phone ready; dimmed), active (a login is in
  progress; badge). Icons are in `windows/Resources/` and `linux/data/tray/`.
- **Menu** (rebuilt each time it opens): a status line, a one-line hint when not ready, then the actions
  that make sense (see `AppDelegate.menuNeedsUpdate`): Cancel <verb>, Check Again; a Phone submenu when
  more than one USB phone is a candidate; Show Activity…;
  Launch at Login; Notify When a Sign-in Arrives; About; Quit. Texts come from
  `ActivityWindowController.describe` (reuse them word for word).
- **Activity window**: headline, one line of explanation, the one or two actions that fit, and a log
  (timestamp + text). First launch opens it once.
- **Preferences** persist: selected USB phone, notifications, launch at login, first-launch flag.
- **Single instance.** A second normal launch activates the first and exits.
- **Background by default**: no taskbar button or window at startup (apart from the first launch).

## Test hooks (the e2e driver sets these; document them, keep them)

- `LOGINRELAY_FAKE_PHONE=<host:port>`: don't touch USB; act as if one USB phone ("Fake Phone") were plugged in
  and "open" it by connecting a plain TCP socket to this address, then run the normal USB-mode logic over it
  (hello, ping/pong, callback/delivered). The same JSON lines, without the 64-byte padding rule.
- `LOGINRELAY_BROWSER=none`: don't open the sign-in page in a browser.
- `LOGINRELAY_LOG=<file>`: also append every activity-log line to this file.

## Verifying a port

1. Pass `tests/vectors.json`.
2. Pass `tests/e2e/run_e2e.py` against the real app (see its header): sign-in with a decoy, sign-out,
   cancel; claim released afterwards.
3. Idle for a few minutes with the fake phone connected and watch the handle count stay flat.
