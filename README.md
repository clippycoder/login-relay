<img src="docs/icon-512.png" alt="" width="96" align="right">

# Login Relay

Sign in to Android apps on a phone that has no web browser.

Many apps (any that use Auth0, AppAuth, or similar) open their sign-in page in a browser or Custom
Tab. On a phone without a browser — a flip phone, a kosher or kids' phone, a debloated ROM — the
app checks for one, finds none, and fails with a generic "Authentication failed" before it sends a
single request.

Login Relay fixes that without putting a browser on the phone: the sign-in page opens in **your
computer's** browser instead, and the result goes back to the app.

```
 phone                                   computer (over the USB cable)
 app: "open the sign-in page"
 Login Relay answers as the browser ──▶  the desktop app opens the page in your normal browser
                                          you sign in there
                                          the site redirects to the app's return address
                                          the desktop app catches it (temporary URL handler)
 Login Relay checks it, gives it   ◀──   the desktop app sends the return address back
 to the app, which finishes the login
```

The page never loads on the phone. The app finishes the login with its own code, so it stores the
session exactly as it would after a normal login.

## Download

From [Releases](https://github.com/clippycoder/login-relay/releases). You need the phone app and one
desktop app.

| | File | Notes |
|---|---|---|
| Phone | `login-relay-<version>.apk` | Android 5.1+. No permissions. |
| macOS | `Login-Relay-macOS-<version>.zip` | Menu bar app, Apple silicon and Intel, macOS 13+ |
| Windows | `Login-Relay-Windows-x64-<version>.zip` | Tray app, Windows 10/11 x64 |
| Linux | `loginrelay_<version>_all.deb` or `loginrelay-<version>-linux.tar.gz` | Tray app, GTK 3 |

Each release lists SHA-256 checksums in `SHA256SUMS`.

**Versions 0.5.0 and later talk to the phone only over USB, in Android's USB accessory mode, and need no
USB debugging.** If you'd rather use adb (USB debugging or wireless debugging), the
[v0.4.0 release](https://github.com/clippycoder/login-relay/releases/tag/v0.4.0) and its phone app still
do that.

## Use it

1. **Install Login Relay on the phone.** It's an ordinary APK with no permissions: install it however you
   install apps (your MDM, a file manager, `adb install`). It's also bundled in each desktop download.
2. **Start the desktop app.** It lives in the menu bar / system tray and shows the phone's state.
3. **Plug the phone in with USB.** If the desktop app says it can't find a phone, pull down the phone's USB
   notification and choose **File transfer**. USB debugging is **not** needed. The first time, Android asks
   whether to let Login Relay use the accessory; tap OK (tick "Use by default" to skip it next time).
4. **Sign in to an app on the phone as usual.** When it opens the sign-in page, Login Relay sends it to
   your computer and your browser opens it. Sign in there. If your browser asks to open "Login Relay",
   allow it. The app on the phone finishes the login.

This is Android's USB accessory mode (the protocol Android Auto uses): the desktop app asks the phone to
re-enumerate as an accessory, then talks to the Login Relay app over it. The phone checks the sign-in
result itself (same return address, same `state`) before handing it to the app. If an adb server is
running and holding the phone, the desktop app stops it (it restarts when needed).

The menu has **Launch at Login**, **Notify When a Sign-in Arrives**, and **Show Activity…**, a window
with a log. On macOS there is also **Show in Dock** (off by default, so it lives only in the menu bar).

### macOS

Unzip and move **Login Relay** to Applications. The app isn't notarized (there's no paid Apple
developer account behind it), so macOS blocks the first launch: right-click the app and choose **Open**,
or open System Settings → Privacy & Security and choose **Open Anyway**, or clear the flag once with
`xattr -dr com.apple.quarantine "/Applications/Login Relay.app"`.

### Windows

Unzip and run `LoginRelay.exe` (keep `login-relay.apk` next to it). It isn't code-signed, so SmartScreen
may say "Windows protected your PC": choose *More info* → *Run anyway*. **Launch at Login** adds a
per-user startup entry.

**Windows drivers.** This needs proper USB drivers, and the app doesn't install them. It talks to the phone
through libusb, which can only open devices that use the **WinUSB** driver, and Windows' stock phone driver
(MTP) isn't one. Install WinUSB for the phone with [Zadig](https://zadig.akeo.ie/) (Options → List All
Devices, pick the phone, choose WinUSB, Replace Driver). After the app first switches the phone into accessory
mode, a second device, "Android Accessory Interface" (USB ID `18D1:2D00`–`2D05`), appears and needs WinUSB
too. If the app says "Can't open the phone", a missing driver is the likely reason. Installing WinUSB for the
phone takes over its normal Windows connection (file transfer) until you restore the driver in Device
Manager. Not yet tried with a real phone on Windows, so treat these steps as unverified.

### Linux

```
sudo apt install ./loginrelay_<version>_all.deb        # Debian, Ubuntu, and derivatives
```

or unpack the tarball and run `./install.sh` for a per-user install under `~/.local`. The package needs
`python3-usb` and installs a udev rule that lets the logged-in user use the phone. The tray icon uses
AppIndicator, so it needs a desktop that shows those: KDE does; **GNOME needs the "AppIndicator and
KStatusNotifierItem Support" extension**. Without a tray the app keeps its window open instead, with
the same actions.

### No desktop app? The command-line helper (adb)

An older Python script does the same job with no tray or window, but over adb (USB debugging on). With
Login Relay installed on the phone and the phone connected over adb:

```
curl -fsSL https://raw.githubusercontent.com/clippycoder/login-relay/main/relay.sh | sh
```

`relay.sh` downloads `host/oauth_relay.py` and runs it, so `python3` must be installed. To pass options,
use `… | sh -s -- --serial 1234ABCD`. Options: `--serial` picks a phone, `--paste` skips the URL handler
and you paste the final address by hand, `--yes` skips the warning for sign-ins without PKCE.

Links that aren't sign-ins, such as a "terms of service" link, also open on the computer.

## Status

| | Phone | Desktop |
|---|---|---|
| macOS | tested on a CAT S22 Flip (Android 11) with real sign-ins and sign-outs, USB debugging on; the USB link also works with USB debugging off | used with real apps; unit tests and an end-to-end test run in CI |
| Linux | same phone, from the Linux USB code run on a Mac: the phone switches to accessory mode and answers, with USB debugging on and off | built and tested in CI on a real Linux machine under a virtual display against a fake phone; **not yet run on a Linux desktop with a real phone** |
| Windows | – | built and tested in CI on a real Windows machine against a fake phone; **not yet run with a real phone** |

Tested with the phone in "File transfer" USB mode. A phone left in "No data transfer" / "Charging only" mode
may not show up until you choose File transfer; that couldn't be tried, because the one test phone switches
itself back to File transfer. Not verified anywhere: *Launch at
Login* and notifications. Reports and fixes are welcome.

## How it stays safe

- **PKCE.** Most sign-ins send a `code_challenge` and keep the matching secret on the phone. The
  code that passes through the computer can't be redeemed without that secret. The app warns
  and asks before relaying a sign-in that doesn't use PKCE.
- **State check.** The computer only accepts a return address whose `state` matches the sign-in it
  opened, and it ignores anything else.
- **Delivered only to the app that asked.** The phone hands the return address to the app that opened
  the page and to no other.
- **Only the cable can talk to the phone app.** Over USB the phone app accepts only the accessory the
  system attached for Login Relay; other apps on the phone can't reach it.
- **Nothing left behind on the computer.** The URL handler is registered for one login and then
  removed. If another app already handled that scheme, it is restored; a claim left by a crash is
  cleaned up on the next launch.
- **The phone vets results.** The phone only hands an app a result that matches the page it opened
  (same return address and `state`), so nothing else on the cable can feed an app a login result.
- **Only web links are opened in the browser.** A `file:` or custom-scheme address supplied by the phone
  is refused, so it can't launch an arbitrary program on the computer.

## How the computer catches the redirect

The app takes over the sign-in's return scheme (for example `com.example.app://…`) for the length of one
login, then puts the previous handler back.

| OS | Mechanism |
|---|---|
| macOS | The running app claims the scheme with LaunchServices and receives the URL as an Apple event. |
| Windows | `HKCU\Software\Classes\<scheme>` pointing at the app, with the previous key backed up and restored. A second launch hands the address to the running instance. |
| Linux | A `.desktop` file with `x-scheme-handler/<scheme>` set as the default with `xdg-mime`, restored afterwards. A second launch hands the address to the running instance over D-Bus. |

Apps that return to an `https://` address (Android App Links) can't be caught this way. For those,
the app asks you to paste the final address from the browser's address bar. It does the same if the
system refuses the claim.

## Managed phones (MDM)

Login Relay is an ordinary app with no permissions, so it can be distributed through an MDM like
any other in-house APK. An MDM that removes sideloaded apps will also remove a copy installed with
`adb install`, so add it through the console instead. Over USB the desktop apps need no developer
options on the phone.

| | |
|---|---|
| Package | `io.github.clippycoder.oauthrelay` |
| Signing certificate SHA-256 | `d4:3d:3e:98:27:12:3a:56:f0:92:39:e5:32:40:a6:94:b1:ff:8a:3f:73:af:0a:a7:2d:92:e5:42:a1:40:c8:b4` |
| Permissions | none |
| Icon for the console | [`docs/icon-512.png`](docs/icon-512.png) |

## Limits

- Apps that check for a specific browser package, or that require a working Custom Tabs service,
  may still refuse.
- Some sign-ins cancel if the app's own screen is resumed before the result arrives. Login Relay
  keeps the screen on while it waits, so a screen lock doesn't cancel the login.
- Low-memory phones kill background apps quickly, and an app killed mid-login forgets the login it
  started. Login Relay is therefore a translucent sheet over the app rather than a full screen. That
  keeps the app "visible" to Android (oom_score_adj 100 rather than 700), so it isn't a kill target
  while you sign in.
- Some apps' terms of service may not allow signing in this way. It only uses each app's normal
  sign-in page and return address, but that's between you and the app.

## Build

```
android/build.sh              # phone app: bare SDK tools (aapt2, javac, d8, zipalign, apksigner); no Gradle
android/tools/make_icons.py   # re-render the icons after changing the design (needs Pillow)
macos/Tests/run.sh            # macOS: unit tests (incl. tests/vectors.json)
macos/build.sh                # macOS: universal app, ad-hoc signed, bundles the phone app if it was built
windows/build.ps1             # Windows: dotnet test, then a single-file exe zipped with the phone app
linux/packaging/build-deb.sh  # Linux: .deb (also build-tarball.sh); python3 -m unittest discover -s linux/tests
```

The Mac app builds with the Command Line Tools alone (`swiftc`); Xcode isn't needed. Set
`CODESIGN_IDENTITY` to sign it with a Developer ID certificate. The Windows app needs the .NET 8 SDK.

Phone app signing uses `KEYSTORE` / `KEYSTORE_PASS` if set. Otherwise it uses `.signing/release.jks` with
`.signing/password` if present; that folder is gitignored. Failing both, it uses the SDK debug key.
Updates must be signed with the same key as the installed version, so keep the release key backed up.

## Testing

- `tests/vectors.json` holds the cases for classifying sign-in pages and matching return addresses. The
  Swift, C# and Python implementations all run it.
- `tests/e2e/` is a fake phone (the USB-mode protocol over a socket) and a driver that plays the browser,
  so a whole desktop app can be tested end to end with no phone: a sign-in with a decoy return address, a sign-out, and a cancel
  from the phone. GitHub Actions runs it against the real app on macOS, Windows and Linux.
- `docs/PORTING.md` describes the behaviour a desktop app must match.

## Protocol

Over the USB cable (Android Open Accessory), messages are newline-delimited JSON:

| direction | message |
|---|---|
| phone → computer | `{"v":1,"type":"hello","transport":"usb","version":…}` once per connection |
| phone → computer | `{"v":1,"type":"open","url":…,"referrer":"android-app://<package>","label":"<app name>"}` |
| phone → computer | `{"type":"cancel"}`, `{"type":"pong"}`, `{"type":"delivered"}`, `{"type":"error","text":…}` |
| computer → phone | `{"type":"status","text":…}`, `{"type":"error","text":…}`, `{"type":"done"}`, `{"type":"ping"}`, `{"type":"callback","url":…}` |

[docs/PORTING.md](docs/PORTING.md#usb-accessory-mode) has the details (switching, framing, liveness).

The phone app (0.4.0) also still serves the older adb transport (an abstract unix socket `@oauthrelay`
reached with `adb forward`) so the v0.4.0 desktop apps and `relay.sh` keep working.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
