// SPDX-License-Identifier: GPL-3.0-or-later
package io.github.clippycoder.oauthrelay;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
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
 * Protocol, newline-delimited JSON:
 *   phone to computer: {"v":1,"type":"open","url":..,"referrer":..,"label":..}, {"type":"cancel"}
 *   computer to phone: {"type":"status","text":..}, {"type":"error","text":..}, {"type":"done"}
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

    private enum State { IDLE, WAITING, SENT, DROPPED }

    private TextView titleView;
    private TextView detailView;
    private View requestCard;
    private TextView requestApp;
    private TextView requestSite;
    private TextView connectionView;
    private Button cancelButton;

    private volatile String pendingUrl;
    private volatile String pendingReferrer;
    private volatile String pendingLabel;
    private volatile boolean closing;
    private volatile OutputStream client;
    private volatile LocalSocket clientSocket;
    private LocalServerSocket server;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Several sign-in SDKs cancel the login if their activity is resumed before the result
        // arrives, which happens when the screen locks and unlocks. Keep the screen on instead.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(R.layout.activity_relay);
        titleView = findViewById(R.id.status_title);
        detailView = findViewById(R.id.status_detail);
        requestCard = findViewById(R.id.request_card);
        requestApp = findViewById(R.id.request_app);
        requestSite = findViewById(R.id.request_site);
        connectionView = findViewById(R.id.connection);
        cancelButton = findViewById(R.id.cancel);
        ((TextView) findViewById(R.id.command)).setText(HOST_COMMAND);
        cancelButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                cancel();
            }
        });

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
    public void onBackPressed() {
        if (pendingUrl != null) {
            cancel();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        stopServer();
        super.onDestroy();
    }

    // ------------------------------------------------------------------ UI

    private void handle(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data == null) {
            show(State.IDLE, null);
            return;
        }
        pendingUrl = data.toString();
        Uri ref = getReferrer();
        pendingReferrer = ref == null ? null : ref.toString();
        String caller = ref != null && "android-app".equals(ref.getScheme()) ? ref.getHost() : null;
        pendingLabel = caller == null ? null : appLabel(caller);
        requestApp.setText(getString(R.string.request_app, appLabel(caller)));
        requestSite.setText(getString(R.string.request_site, data.getHost()));
        Log.i(TAG, "page from " + caller + " for host " + data.getHost());
        if (client != null) {
            sendOpen();
        } else {
            show(State.WAITING, null);
        }
    }

    private String appLabel(String pkg) {
        if (pkg == null) {
            return getString(R.string.unknown_app);
        }
        // The app that started us is visible to us even under Android 11 package visibility.
        try {
            PackageManager pm = getPackageManager();
            ApplicationInfo ai = pm.getApplicationInfo(pkg, 0);
            return pm.getApplicationLabel(ai).toString();
        } catch (PackageManager.NameNotFoundException e) {
            return pkg;
        }
    }

    private void show(final State state, final String detailOverride) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                int title;
                int detail;
                switch (state) {
                    case WAITING:
                        title = R.string.waiting_title;
                        detail = R.string.waiting_detail;
                        break;
                    case SENT:
                        title = R.string.sent_title;
                        detail = R.string.sent_detail;
                        break;
                    case DROPPED:
                        title = R.string.dropped_title;
                        detail = R.string.dropped_detail;
                        break;
                    default:
                        title = R.string.idle_title;
                        detail = R.string.idle_detail;
                }
                titleView.setText(title);
                if (detailOverride != null) {
                    detailView.setText(detailOverride);
                } else {
                    detailView.setText(detail);
                }
                boolean active = state != State.IDLE;
                requestCard.setVisibility(active ? View.VISIBLE : View.GONE);
                cancelButton.setVisibility(active ? View.VISIBLE : View.GONE);
                updateConnection();
            }
        });
    }

    private void updateConnection() {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                boolean on = client != null;
                connectionView.setText(on ? R.string.computer_connected : R.string.computer_waiting);
                // Resources.getColor(int) rather than Context.getColor: the latter needs API 23.
                connectionView.setTextColor(
                        getResources().getColor(on ? R.color.ok : R.color.warn));
            }
        });
    }

    private void setDetail(final String text) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                detailView.setText(text);
            }
        });
    }

    private void cancel() {
        pendingUrl = null;
        send("{\"type\":\"cancel\"}");
        // Finishing returns to the app, whose sign-in screen treats it as "user cancelled".
        finish();
    }

    // ------------------------------------------------------------------ socket

    private void startServer() {
        try {
            server = new LocalServerSocket(SOCKET_NAME);
        } catch (IOException e) {
            Log.e(TAG, "cannot bind @" + SOCKET_NAME, e);
            show(State.IDLE, getString(R.string.bind_failed));
            return;
        }
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                acceptLoop();
            }
        }, "oauthrelay-server");
        t.setDaemon(true);
        t.start();
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
        // Also drop the computer's connection; otherwise the helper stays attached to this dead
        // instance and never reaches the next Login Relay screen.
        // close() alone neither wakes the thread blocked reading it nor sends EOF to adb;
        // shutdown does both.
        LocalSocket c = clientSocket;
        if (c != null) {
            try {
                c.shutdownInput();
            } catch (IOException ignored) {
            }
            try {
                c.shutdownOutput();
            } catch (IOException ignored) {
            }
            closeQuietly(c);
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
        clientSocket = s;
        try {
            client = s.getOutputStream();
            updateConnection();
            if (pendingUrl != null) {
                sendOpen();
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
            clientSocket = null;
            closeQuietly(s);
            if (!closing) {
                if (pendingUrl != null) {
                    show(State.DROPPED, null);
                } else {
                    updateConnection();
                }
            }
        }
    }

    private void onMessage(String line) {
        try {
            JSONObject m = new JSONObject(line);
            String type = m.optString("type");
            if ("status".equals(type) || "error".equals(type)) {
                setDetail(m.optString("text"));
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

    private void sendOpen() {
        String url = pendingUrl;
        if (url == null) {
            return;
        }
        try {
            JSONObject m = new JSONObject();
            m.put("v", 1);
            m.put("type", "open");
            m.put("url", url);
            m.put("referrer", pendingReferrer == null ? JSONObject.NULL : pendingReferrer);
            m.put("label", pendingLabel == null ? JSONObject.NULL : pendingLabel);
            if (send(m.toString())) {
                show(State.SENT, null);
            }
        } catch (JSONException e) {
            Log.w(TAG, "encode failed", e);
        }
    }

    private synchronized boolean send(String json) {
        OutputStream out = client;
        if (out == null) {
            return false;
        }
        try {
            out.write((json + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
            return true;
        } catch (IOException e) {
            Log.w(TAG, "send failed", e);
            return false;
        }
    }

    private static void closeQuietly(LocalSocket s) {
        try {
            s.close();
        } catch (IOException ignored) {
        }
    }
}
