# Login Relay

Sign in to Android apps on a phone that has no web browser.

Many apps (any that use Auth0, AppAuth, or similar) open their sign-in page in a browser or Custom
Tab. On a phone without a browser — a flip phone, a kosher or kids' phone, a debloated ROM — the
app checks for one, finds none, and fails with a generic "Authentication failed" before it sends a
single request.

Login Relay fixes that without putting a browser on the phone:

```
 phone                                   computer (over adb)
 app: "open the sign-in page"
 Login Relay answers as the browser ──▶  oauth_relay.py opens the page in your normal browser
                                          you sign in there
                                          the site redirects to the app's return address
                                          oauth_relay.py catches it (temporary URL handler)
 app finishes the sign-in itself   ◀──   adb shell am start <return address> -p <app>
```

The page never loads on the phone. The app finishes the login with its own code, so it stores the
session exactly as it would after a normal login.

## Use it

1. Install the app on the phone (enable USB debugging first):

   ```
   adb install login-relay.apk
   ```

2. With the phone connected (USB or wireless debugging), run the helper on the computer:

   ```
   gh api -H 'Accept: application/vnd.github.raw' repos/clippycoder/oauth-relay/contents/relay.sh | sh
   ```

   The repo is private for now, so the one-liner goes through `gh`. Once the repo is public it
   becomes `curl -fsSL https://raw.githubusercontent.com/clippycoder/oauth-relay/main/relay.sh | sh`.
   `relay.sh` downloads `host/oauth_relay.py` and runs it, so `python3` must be installed. On macOS
   that comes with the Command Line Tools (`xcode-select --install`). To pass options, use
   `… | sh -s -- --serial 1234ABCD`. From a clone, run `./relay.sh` or `python3 host/oauth_relay.py`.

3. In the app, sign in as usual. When it opens the sign-in page, Login Relay sends it to the computer.
   Your browser opens it. Sign in, and allow the browser to open "Login Relay" when it asks. The app
   on the phone completes the login.

Options: `--serial` picks a phone when several are connected. `--paste` skips the URL handler, and
you paste the final address by hand instead. `--yes` skips the warning for sign-ins without PKCE.

Links that aren't sign-ins, such as a "terms of service" link, also open on the computer.

## How it stays safe

- **PKCE.** Most sign-ins send a `code_challenge` and keep the matching secret on the phone. The
  code that passes through the computer can't be redeemed without that secret. The helper warns
  and asks before relaying a sign-in that doesn't use PKCE.
- **State check.** The helper only accepts a return address whose `state` matches the sign-in it
  opened, and it ignores anything else.
- **Delivered only to the app that asked.** The return address goes back with `-p <package>` of
  the app that opened the page.
- **Only adb can talk to the phone app.** Login Relay serves on an abstract unix socket, and it
  refuses peers that aren't the `shell` or `root` user, so other apps can't read the pages.
- **Nothing left behind on the computer.** The URL handler is registered for one login and then
  removed. If another app already handled that scheme, the helper restores it.

## How the computer catches the redirect

| OS | Mechanism | Status |
|---|---|---|
| macOS | A throwaway AppleScript applet in `~/Library/Caches/oauth-relay/` that claims the scheme. It's registered with `lsregister`, made the default via LaunchServices, and restored afterwards. | tested |
| Linux | A `.desktop` file with `x-scheme-handler/<scheme>` and `xdg-mime default`, restored afterwards | untested |
| Windows | `HKCU\Software\Classes\<scheme>` pointing at a tiny `pythonw` handler, with the previous key backed up and restored | untested |

Apps that return to an `https://` address (Android App Links) can't be caught this way. For those,
the helper asks you to paste the final address from the browser's address bar.

## Limits

- The phone app is an ordinary install. An MDM that removes sideloaded apps will remove it too.
- Apps that check for a specific browser package, or that require a working Custom Tabs service,
  may still refuse.
- Some sign-ins cancel if the app's own screen is resumed before the result arrives. Login Relay
  keeps the screen on while it waits, so a screen lock doesn't cancel the login.

## Build

```
android/build.sh          # bare SDK tools (aapt2, javac, d8, zipalign, apksigner); no Gradle
```

It signs with `KEYSTORE` / `KEYSTORE_PASS` if set, otherwise with the SDK debug key.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
