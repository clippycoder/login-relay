# Porting the desktop app

The macOS app (`macos/Sources/*.swift`) is the reference implementation. A port on another system
must behave the same way; the strings and timings below are the contract. When this file and the
Swift code disagree, the Swift code wins (and this file needs fixing).

Three things are shared on purpose:

- `tests/vectors.json`: how sign-in pages are classified and how return addresses are matched.
  Every implementation must pass all of it.
- `tests/e2e/`: a fake phone (`fake_phone.py`) and a fake adb (`fake_adb.py`) plus a driver
  (`run_e2e.py`) that tests a whole app without a phone.
- The phone protocol, below.

## What the app does

A tray / menu bar app that, when an Android app on a USB-connected phone asks to open a web page
(Login Relay on the phone forwards it), opens the page in the computer's browser, catches the
redirect back to the app, and gives it to the phone with `adb`.

```
Login Relay (phone) <-- adb forward tcp:N localabstract:oauthrelay --> the app
   {"type":"open",url,referrer,label}  ------------------------------>  open url in the browser
                                                                         claim the return scheme
                                                                         wait for the redirect
   adb shell am start -a android.intent.action.VIEW -d <return> -p <pkg> <------ callback arrives
   {"type":"done"}  <-------------------------------------------------  (after 1.5 s)
```

## Phone protocol

Newline-delimited JSON over the forwarded socket.

| direction | message |
|---|---|
| phone → app | `{"v":1,"type":"open","url":…,"referrer":"android-app://<package>","label":"<app name>"}`; `referrer` and `label` may be `null` or missing |
| phone → app | `{"type":"cancel"}` |
| app → phone | `{"type":"status","text":…}`, `{"type":"error","text":…}`, `{"type":"done"}` (closes the phone's screen) |

Unknown messages are ignored. The phone resends its pending `open` to every new connection until it
gets `done`, so a reconnect legitimately starts the same login again.

**adb accepts the TCP connection even when nothing listens on the phone, then closes it.** So a
connect only counts as "linked" if it stays open for ~400 ms.

## Pure logic (port this first; test it with the vectors)

`Redirect.swift` has everything: `urlParams`, `sameRedirect`, `RelayRequest` (kind, redirect, state,
caller, label, appName, catchableScheme, lacksPKCE, host, `accepts(callback:)`), `shellQuote`,
`versionLess`.

- Sign-in uses `redirect_uri`; sign-out uses `post_logout_redirect_uri` or Auth0's `returnTo`; anything
  else is a plain link.
- `accepts`: `unrelated` unless the callback equals the redirect address (scheme case-insensitive,
  query and fragment ignored, trailing slashes ignored); then `stateMismatch` if the request has a
  `state` and the callback's differs or is missing; else `ok`.
- `shellQuote` wraps in single quotes and writes each `'` as `'"'"'`. `adb shell` re-parses its
  argument on the phone.

## Engine

State lives on the UI thread; one background thread polls adb. See `RelayEngine.swift`.

**Finding adb**, first match wins: `LOGINRELAY_ADB` (test hook, returned as is); the path the user
picked; the copy this app downloaded into its own data folder; `$ANDROID_HOME` / `$ANDROID_SDK_ROOT`
`/platform-tools/adb`; the usual SDK and package-manager locations; then `PATH`. GUI apps start with
a minimal `PATH`, so look in the usual places explicitly.

**Running adb: never wait for end-of-file on its output.** When adb has to start its server it forks a
daemon that inherits the output handles, so "read until EOF" blocks forever. Read via temp files, or
wait for the process to exit and then give the streams at most ~500 ms. Kill the process after the
timeout (20 s; 15 s for `devices`). And **close every handle, process and socket you open**: the
first macOS build leaked two file descriptors a second and silently died after two minutes. Verify by
watching the process's handle count while idle.

**Poll loop** (background thread, ticks every second):

1. Every 3 s, or when "Check Again" is pressed: locate adb; `adb devices -l` (ignore lines starting
   with `*` and the `List of devices attached` header; fields are serial, state, then `model:NAME`);
   choose the phone: the one the user picked (by serial) if usable, else the first with state
   `device`; `adb shell dumpsys package io.github.clippycoder.oauthrelay` and read `versionName=`
   (none → the phone app isn't installed); reuse the existing forward from `adb forward --list`
   (`<serial> tcp:<port> localabstract:oauthrelay`) or create one with
   `adb -s <serial> forward tcp:0 localabstract:oauthrelay` (prints the port). Publish the result to
   the UI thread.
2. If a phone with the phone app and a forward exists, try to link (connect to `127.0.0.1:<port>`).
   On failure sleep 1 s and retry. On success, publish "linked", then wait for the link to end,
   **re-checking every 3 s that the phone is still listed** and hanging up if it isn't. When the link
   ends, check the phone again immediately.
3. A watchdog on the UI thread: if the loop has made no progress for 30 s, log it, close the link, and
   start a new loop (abandon the old one). The progress marker must be written by the loop thread
   itself, so a modal dialog on the UI thread can't trip the watchdog.

**Phases** (derived, not stored): no adb → no phone (adb lists nothing) → phone not ready (adb lists a
phone whose state isn't `device`: `unauthorized`, `offline`, …) → phone app missing → ready → working
(a login is in progress).

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
   "Returning to <app> on the phone…", run `adb -s <serial> shell` with the single argument
   `am start -a android.intent.action.VIEW -d <shellQuote(url)> -p <shellQuote(package)>` (omit `-p`
   if the package is unknown), off the UI thread. If the output contains `Error` or the exit code is
   non-zero: log it and send `error` "Couldn't hand the result back to the app." Otherwise log
   "Done: <app> is finishing the <verb>." and send `done` after 1.5 s.
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

**Phone app management**: if the bundled `login-relay.apk` exists, offer "Install Login Relay on
Phone" when it's missing and "Update …" when `versionLess(installed, bundled)`; run
`adb -s <serial> install -r <apk>` (success = output contains `Success`). "Open Login Relay on Phone":
`input keyevent KEYCODE_WAKEUP; am start -n io.github.clippycoder.oauthrelay/.RelayActivity`.

**Downloading platform-tools** (only after the user confirms; Android SDK License): fetch
`https://dl.google.com/android/repository/repository2-3.xml`, find the `platform-tools` package, take the
archive for this OS (`darwin`/`win`/`linux` in the file name) and its `sha1`, download
`https://dl.google.com/android/repository/<file>`, **refuse it if the SHA-1 differs**, unpack into the
app's own data folder.

## Interface

- **Tray / menu bar icon**: three states: ready, off (no phone ready; dimmed), active (a login is in
  progress; badge). Icons are in `windows/Resources/` and `linux/data/tray/`.
- **Menu** (rebuilt each time it opens): a status line, a one-line hint when not ready, then the actions
  that make sense (see `AppDelegate.menuNeedsUpdate`): Download Android Platform-Tools… / Choose adb…,
  Install Login Relay on Phone, Open Login Relay on Phone, Update Login Relay on Phone (x → y),
  Cancel <verb>, Check Again; a Phone submenu when more than one phone is usable; Show Activity…;
  Launch at Login; Notify When a Sign-in Arrives; About; Quit. Texts come from
  `ActivityWindowController.describe` (reuse them word for word).
- **Activity window**: headline, one line of explanation, the one or two actions that fit, and a log
  (timestamp + text). First launch opens it once.
- **Preferences** persist: user-chosen adb path, selected phone serial, notifications, launch at login,
  first-launch flag.
- **Single instance.** A second normal launch activates the first and exits.
- **Background by default**: no taskbar button or window at startup (apart from the first launch).

## Test hooks (the e2e driver sets these; document them, keep them)

- `LOGINRELAY_ADB=<path>`: use exactly this adb.
- `LOGINRELAY_BROWSER=none`: don't open the sign-in page in a browser.
- `LOGINRELAY_LOG=<file>`: also append every activity-log line to this file.
- `LOGINRELAY_TRANSPORT=adb|usb`: pin the connection type, overriding the saved choice (apps without USB mode ignore it).

## Verifying a port

1. Pass `tests/vectors.json`.
2. Pass `tests/e2e/run_e2e.py` against the real app (see its header): sign-in with a decoy, sign-out,
   cancel; claim released afterwards.
3. Idle for a few minutes with the fake phone connected and watch the handle count stay flat.
