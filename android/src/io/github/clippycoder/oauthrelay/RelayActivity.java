// SPDX-License-Identifier: GPL-3.0-or-later
package io.github.clippycoder.oauthrelay;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Typeface;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Stands in for a browser. When an app asks to open a web page (usually an OAuth sign-in page),
 * this activity holds it and serves it on the abstract unix socket "oauthrelay". The computer-side
 * helper reaches that socket through `adb forward`, opens the page in a real browser, catches the
 * redirect, and sends it back to the app with `adb shell am start`. The page never loads on the
 * phone.
 *
 * Protocol: newline-delimited JSON. Phone to computer: {"v":1,"type":"open","url":..,"referrer":..}.
 * Computer to phone: {"type":"status","text":..}, {"type":"done"}, {"type":"error","text":..}.
 */
public class RelayActivity extends Activity {
    static final String TAG = "OAuthRelay";
    static final String SOCKET_NAME = "oauthrelay";
    static final String HOST_COMMAND =
            "gh api -H 'Accept: application/vnd.github.raw' "
            + "repos/clippycoder/oauth-relay/contents/relay.sh | sh";

    // Only adb (shell) and root may talk to the relay; any app could otherwise connect to an
    // abstract socket.
    private static final int UID_ROOT = 0;
    private static final int UID_SHELL = 2000;

    private TextView statusView;
    private TextView urlView;
    private TextView commandView;

    private volatile String pendingUrl;
    private volatile String pendingReferrer;
    private volatile boolean closing;
    private volatile OutputStream client;
    private LocalServerSocket server;
    private Thread serverThread;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Several sign-in SDKs cancel the login if their activity is resumed before the result
        // arrives, which happens when the screen locks and unlocks. Keep the screen on instead.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        buildUi();
        startServer();
        handle(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handle(intent);
    }

    @Override
    protected void onDestroy() {
        stopServer();
        super.onDestroy();
    }

    private void buildUi() {
        int pad = dp(16);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("Login Relay");
        title.setTextSize(22);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        col.addView(title);

        statusView = text(col, 18);
        urlView = text(col, 14);

        TextView hint = text(col, 14);
        hint.setText("\nOn a computer with this phone connected over adb, run:");
        commandView = text(col, 13);
        commandView.setTypeface(Typeface.MONOSPACE);
        commandView.setTextIsSelectable(true);
        commandView.setText(HOST_COMMAND);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(col);
        setContentView(scroll);
    }

    private TextView text(LinearLayout parent, int sp) {
        TextView v = new TextView(this);
        v.setTextSize(sp);
        v.setPadding(0, dp(8), 0, 0);
        parent.addView(v);
        return v;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private void handle(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data == null) {
            setStatus("This phone has no web browser. When an app wants to open a sign-in page, "
                    + "Login Relay sends it to your computer instead.");
            urlView.setText("");
            return;
        }
        pendingUrl = data.toString();
        Uri ref = getReferrer();
        pendingReferrer = ref == null ? null : ref.toString();
        urlView.setText(data.getHost());
        Log.i(TAG, "page from " + pendingReferrer + " for host " + data.getHost());
        if (client != null) {
            sendOpen();
        } else {
            setStatus("Continue on your computer. Waiting for it to connect…");
        }
    }

    private void setStatus(final String s) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                statusView.setText(s);
            }
        });
    }

    private void startServer() {
        try {
            server = new LocalServerSocket(SOCKET_NAME);
        } catch (IOException e) {
            Log.e(TAG, "cannot bind @" + SOCKET_NAME, e);
            setStatus("Could not start the relay: " + e.getMessage());
            return;
        }
        serverThread = new Thread(new Runnable() {
            @Override
            public void run() {
                acceptLoop();
            }
        }, "oauthrelay-server");
        serverThread.setDaemon(true);
        serverThread.start();
    }

    private void stopServer() {
        closing = true;
        if (server == null) {
            return;
        }
        // LocalServerSocket.close() does not unblock a pending accept(); poke it with a connection.
        try {
            LocalSocket poke = new LocalSocket();
            poke.connect(new LocalSocketAddress(SOCKET_NAME));
            poke.close();
        } catch (IOException ignored) {
        }
        try {
            server.close();
        } catch (IOException ignored) {
        }
    }

    private void acceptLoop() {
        while (!closing) {
            LocalSocket s;
            try {
                s = server.accept();
            } catch (IOException e) {
                if (!closing) {
                    Log.e(TAG, "accept failed", e);
                }
                return;
            }
            if (closing) {
                closeQuietly(s);
                return;
            }
            int uid;
            try {
                uid = s.getPeerCredentials().getUid();
            } catch (IOException e) {
                closeQuietly(s);
                continue;
            }
            if (uid != UID_SHELL && uid != UID_ROOT) {
                Log.w(TAG, "refused connection from uid " + uid);
                closeQuietly(s);
                continue;
            }
            serve(s);
        }
    }

    private void serve(LocalSocket s) {
        try {
            client = s.getOutputStream();
            if (pendingUrl != null) {
                sendOpen();
            } else {
                setStatus("Computer connected. Waiting for an app to open a sign-in page.");
            }
            BufferedReader in = new BufferedReader(
                    new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
            String line;
            while ((line = in.readLine()) != null) {
                onMessage(line);
            }
        } catch (IOException e) {
            Log.w(TAG, "client dropped", e);
        } finally {
            client = null;
            closeQuietly(s);
            if (!closing && pendingUrl != null) {
                setStatus("Computer disconnected. Run the command again to continue.");
            }
        }
    }

    private void onMessage(String line) {
        try {
            JSONObject m = new JSONObject(line);
            String type = m.optString("type");
            if ("status".equals(type) || "error".equals(type)) {
                setStatus(m.optString("text"));
            } else if ("done".equals(type)) {
                pendingUrl = null;
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        finish();
                    }
                });
            }
        } catch (JSONException e) {
            Log.w(TAG, "bad message", e);
        }
    }

    private synchronized void sendOpen() {
        OutputStream out = client;
        String url = pendingUrl;
        if (out == null || url == null) {
            return;
        }
        try {
            JSONObject m = new JSONObject();
            m.put("v", 1);
            m.put("type", "open");
            m.put("url", url);
            m.put("referrer", pendingReferrer == null ? JSONObject.NULL : pendingReferrer);
            out.write((m.toString() + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
            setStatus("Sent to your computer. Finish signing in there.");
        } catch (IOException | JSONException e) {
            Log.w(TAG, "send failed", e);
        }
    }

    private static void closeQuietly(LocalSocket s) {
        try {
            s.close();
        } catch (IOException ignored) {
        }
    }
}
