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
 phone                                   computer (over adb)
 app: "open the sign-in page"
 Login Relay answers as the browser ──▶  the desktop app opens the page in your normal browser
                                          you sign in there
                                          the site redirects to the app's return address
                                          the desktop app catches it (temporary URL handler)
 app finishes the sign-in itself   ◀──   adb shell am start <return address> -p <app>
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

## Use it

Plug the phone in with USB and turn on USB debugging (Settings → Developer options); accept the
"Allow USB debugging?" prompt on the phone. Then:

1. **Start the desktop app.** It lives in the menu bar / system tray and shows the phone's state.
2. **Install Login Relay on the phone**: the desktop app bundles the APK and offers to install it
   (menu → *Install Login Relay on Phone*).
3. **Sign in to an app on the phone as usual.** When it opens the sign-in page, Login Relay sends it
   to your computer and your browser opens it. Sign in there. If your browser asks to open "Login
   Relay", allow it. The app on the phone finishes the login.

If adb isn't installed, the desktop app offers to download Google's platform-tools (checked against
Google's published checksum). You can also point it at an adb you already have.

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

### Linux

```
sudo apt install ./loginrelay_<version>_all.deb        # Debian, Ubuntu, and derivatives
```

or unpack the tarball and run `./install.sh` for a per-user install under `~/.local`. The tray icon uses
AppIndicator, so it needs a desktop that shows those: KDE does; **GNOME needs the "AppIndicator and
KStatusNotifierItem Support" extension**. Without a tray the app keeps its window open instead, with
the same actions.

### No desktop app? The command-line helper

A Python script does the same job with no tray or window. With Login Relay installed on the phone and
the phone connected over adb:

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
| macOS | tested on a CAT S22 Flip (Android 11) with real sign-ins and sign-outs | used with real apps; unit tests and an end-to-end test run in CI |
| Windows | – | built and tested in CI on a real Windows machine against a fake phone; **not yet run with a real phone** |
| Linux | – | built and tested in CI on a real Linux machine under a virtual display against a fake phone; **not yet run with a real phone** |

Not verified anywhere: *Launch at Login*, notifications, and the platform-tools download. Reports and
fixes are welcome.

## How it stays safe

- **PKCE.** Most sign-ins send a `code_challenge` and keep the matching secret on the phone. The
  code that passes through the computer can't be redeemed without that secret. The app warns
  and asks before relaying a sign-in that doesn't use PKCE.
- **State check.** The computer only accepts a return address whose `state` matches the sign-in it
  opened, and it ignores anything else.
- **Delivered only to the app that asked.** The return address goes back with `-p <package>` of
  the app that opened the page.
- **Only adb can talk to the phone app.** Login Relay serves on an abstract unix socket, and it
  refuses peers that aren't the `shell` or `root` user, so other apps can't read the pages.
- **Nothing left behind on the computer.** The URL handler is registered for one login and then
  removed. If another app already handled that scheme, it is restored; a claim left by a crash is
  cleaned up on the next launch.
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
`adb install`, so add it through the console instead.

| | |
|---|---|
| Package | `io.github.clippycoder.oauthrelay` |
| Signing certificate SHA-256 | `d4:3d:3e:98:27:12:3a:56:f0:92:39:e5:32:40:a6:94:b1:ff:8a:3f:73:af:0a:a7:2d:92:e5:42:a1:40:c8:b4` |
| Permissions | none |
| Icon for the console | [`docs/icon-512.png`](docs/icon-512.png) |

The phone also needs USB debugging (or wireless debugging) allowed while you sign in, because the
hand-off runs over adb.

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
- `tests/e2e/` is a fake phone, a fake adb and a driver that plays the browser, so a whole desktop app
  can be tested end to end with no phone: a sign-in with a decoy return address, a sign-out, and a cancel
  from the phone. GitHub Actions runs it against the real app on macOS, Windows and Linux.
- `docs/PORTING.md` describes the behaviour a desktop app must match.

## Protocol

The phone app listens on the abstract unix socket `@oauthrelay` and accepts only uid 0 or 2000.
The computer reaches it with `adb forward tcp:0 localabstract:oauthrelay`. Messages are newline-delimited
JSON:

| direction | message |
|---|---|
| phone → computer | `{"v":1,"type":"open","url":…,"referrer":"android-app://<package>","label":"<app name>"}` (`label` since 0.3.0) |
| phone → computer | `{"type":"cancel"}` |
| computer → phone | `{"type":"status","text":…}`, `{"type":"error","text":…}`, `{"type":"done"}` |

The return address goes back with `adb shell am start -a android.intent.action.VIEW -d <url> -p <package>`.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
