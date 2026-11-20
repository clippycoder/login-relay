// SPDX-License-Identifier: GPL-3.0-or-later
package io.github.clippycoder.oauthrelay;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.ActivityNotFoundException;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.hardware.usb.UsbAccessory;
import android.hardware.usb.UsbManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.TextView;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/**
 * Stands in for a browser. When an app asks to open a web page (usually an OAuth sign-in page),
 * this activity holds it and hands it to a Login Relay desktop app, which opens it in a real
 * browser and catches the redirect. The page never loads on the phone.
 *
 * The computer is reached over USB accessory mode (AOA), carrying newline-delimited JSON. The
 * computer switches the phone into accessory mode over the cable and Android connects this app to
 * it. The computer sends the redirect as a "callback" message and the phone hands it to the app
 * itself.
 *
 * Messages:
 *   phone to computer: {"v":1,"type":"hello","transport":..,"version":..},
 *                      {"v":1,"type":"open","url":..,"referrer":..,"label":..}, {"type":"cancel"},
 *                      {"type":"pong"}, {"type":"delivered"}
 *   computer to phone: {"type":"status","text":..}, {"type":"error","text":..}, {"type":"done"},
 *                      {"type":"callback","url":..}, {"type":"ping"}
 */
public class RelayActivity extends Activity {
    static final String TAG = "OAuthRelay";
    static final String ACTION_USB_PERMISSION = "io.github.clippycoder.oauthrelay.USB_PERMISSION";
    // Where to get the computer side. Shown on the phone's own screen.
    static final String HOST_COMMAND = "github.com/clippycoder/login-relay";

    // Reads from an accessory must use a buffer of at least 16 KiB: a partial read drops the rest of
    // the transfer.
    private static final int READ_BUFFER = 16384;
    private static final int MAX_LINE = 1024 * 1024;
    private static final long FINISH_DELAY_MS = 1500;

    private enum State { IDLE, WAITING, SENT, DROPPED }

    /** The USB link outlives any one screen; see UsbLink. */
    private static volatile UsbLink sUsb;

    private final Handler main = new Handler(Looper.getMainLooper());

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
    private volatile Link link;
    private Link helloedLink;  // the link we've already said hello on, so a re-adopt doesn't repeat it
    private BroadcastReceiver permissionReceiver;

    // ------------------------------------------------------------------ lifecycle

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

        adoptUsb(null);
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
        closing = true;
        // The USB link stays open (see UsbLink); it just has no screen to report to for now.
        UsbLink u = sUsb;
        if (u != null && u.owner == this) {
            u.owner = null;
        }
        if (permissionReceiver != null) {
            try {
                unregisterReceiver(permissionReceiver);
            } catch (IllegalArgumentException ignored) {
            }
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------ UI

    private void handle(Intent intent) {
        if (intent != null && UsbManager.ACTION_USB_ACCESSORY_ATTACHED.equals(intent.getAction())) {
            // The computer just switched this phone into accessory mode. Don't touch the sign-in UI.
            adoptUsb((UsbAccessory) intent.getParcelableExtra(UsbManager.EXTRA_ACCESSORY));
            return;
        }
        Uri data = intent == null ? null : intent.getData();
        if (data == null) {
            show(State.IDLE, null);
            return;
        }
        pendingUrl = data.toString();
        Uri ref = getReferrer();
        pendingReferrer = ref == null ? null : ref.toString();
        String caller = callerPackage();
        pendingLabel = caller == null ? null : appLabel(caller);
        requestApp.setText(getString(R.string.request_app, appLabel(caller)));
        requestSite.setText(getString(R.string.request_site, data.getHost()));
        Log.i(TAG, "page from " + caller + " for host " + data.getHost());
        if (link != null) {
            sendOpen();
        } else {
            show(State.WAITING, null);
        }
    }

    /** Package name of the app that asked for the page, from the "android-app://<package>" referrer. */
    private String callerPackage() {
        String r = pendingReferrer;
        if (r == null || !r.startsWith("android-app://")) {
            return null;
        }
        String rest = r.substring("android-app://".length());
        int slash = rest.indexOf('/');
        if (slash >= 0) {
            rest = rest.substring(0, slash);
        }
        return rest.isEmpty() ? null : rest;
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
                Link l = link;
                boolean on = l != null;
                connectionView.setText(on ? l.describe(RelayActivity.this) : getString(R.string.computer_waiting));
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

    // ------------------------------------------------------------------ links

    /** A connection to a computer. */
    private interface Link {
        boolean send(String json);

        String describe(Context context);

        String transport();
    }

    private interface LineSink {
        void onLine(String line);
    }

    /** Splits a byte stream into UTF-8 lines. Reads in 16 KiB chunks, as accessory mode requires. */
    private static void pumpLines(InputStream in, LineSink sink) throws IOException {
        byte[] buf = new byte[READ_BUFFER];
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        int n;
        while ((n = in.read(buf)) >= 0) {
            for (int i = 0; i < n; i++) {
                byte b = buf[i];
                if (b == '\n') {
                    if (line.size() > 0) {
                        String s = new String(line.toByteArray(), StandardCharsets.UTF_8);
                        line.reset();
                        sink.onLine(s);
                    }
                } else if (line.size() >= MAX_LINE) {
                    throw new IOException("line too long");
                } else {
                    line.write(b);
                }
            }
        }
    }

    private synchronized boolean send(String json) {
        Link l = link;
        return l != null && l.send(json);
    }

    /** Called when a computer connects, or when this screen takes over an existing connection. */
    private void onLinkUp() {
        updateConnection();
        Link l = link;
        if (l != null && l != helloedLink) {
            helloedLink = l;
            try {
                JSONObject hello = new JSONObject();
                hello.put("v", 1);
                hello.put("type", "hello");
                hello.put("transport", l.transport());
                hello.put("version", versionName());
                send(hello.toString());
            } catch (JSONException e) {
                Log.w(TAG, "encode failed", e);
            }
        }
        if (pendingUrl != null) {
            sendOpen();
        }
    }

    private String versionName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "";
        }
    }

    /** The link ended. Called on the link's reader thread. */
    private void linkLost(Link l) {
        if (link != l) {
            return;
        }
        link = null;
        if (!closing) {
            if (pendingUrl != null) {
                show(State.DROPPED, null);
            } else {
                updateConnection();
            }
        }
    }

    // ------------------------------------------------------------------ USB accessory link

    /**
     * The connection over a USB accessory. It is deliberately NOT tied to one screen: the accessory
     * file can be opened by only one reader at a time, and a read blocked in the kernel can't be
     * interrupted, so closing it when a screen goes away would leave it busy until the cable is
     * unplugged. Instead one UsbLink and its reader thread live for as long as the accessory stays
     * attached; whichever screen is current is its "owner" and receives the messages. While no screen
     * owns it, it still answers pings, so the computer can see this app is alive.
     */
    private static final class UsbLink implements Link, LineSink {
        private final ParcelFileDescriptor pfd;
        private final FileInputStream in;
        private final FileOutputStream out;
        private final String description;
        volatile RelayActivity owner;
        volatile boolean dead;

        UsbLink(ParcelFileDescriptor pfd, String description) {
            this.pfd = pfd;
            this.in = new FileInputStream(pfd.getFileDescriptor());
            this.out = new FileOutputStream(pfd.getFileDescriptor());
            this.description = description == null ? "" : description;
        }

        void start() {
            Thread t = new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        pumpLines(in, UsbLink.this);
                    } catch (IOException e) {
                        Log.i(TAG, "usb link ended: " + e.getMessage());
                    } finally {
                        dead = true;
                        try {
                            pfd.close();
                        } catch (IOException ignored) {
                        }
                        if (sUsb == UsbLink.this) {
                            sUsb = null;
                        }
                        RelayActivity o = owner;
                        if (o != null) {
                            o.linkLost(UsbLink.this);
                        }
                    }
                }
            }, "oauthrelay-usb");
            t.setDaemon(true);
            t.start();
        }

        @Override
        public void onLine(String line) {
            if (line.contains("\"ping\"")) {
                try {
                    if ("ping".equals(new JSONObject(line).optString("type"))) {
                        send("{\"type\":\"pong\"}");
                        return;
                    }
                } catch (JSONException ignored) {
                }
            }
            RelayActivity o = owner;
            if (o != null) {
                o.onMessage(line);
            }
        }

        @Override
        public synchronized boolean send(String json) {
            if (dead) {
                return false;
            }
            byte[] data = (json + "\n").getBytes(StandardCharsets.UTF_8);
            // A bulk transfer whose length is an exact multiple of the packet size (64, 512 or 1024)
            // ends without a short packet, and the other side's read doesn't complete. One more newline
            // (a blank line, which the reader skips) avoids that.
            if (data.length % 64 == 0) {
                byte[] padded = new byte[data.length + 1];
                System.arraycopy(data, 0, padded, 0, data.length);
                padded[data.length] = '\n';
                data = padded;
            }
            try {
                out.write(data);
                return true;
            } catch (IOException e) {
                Log.w(TAG, "usb send failed", e);
                return false;
            }
        }

        @Override
        public String describe(Context c) {
            return description.isEmpty()
                    ? c.getString(R.string.computer_connected_usb)
                    : c.getString(R.string.computer_connected_usb_named, description);
        }

        @Override
        public String transport() {
            return "usb";
        }
    }

    /** Takes over the live USB link, or opens the accessory if one is attached and permitted. */
    private void adoptUsb(UsbAccessory attached) {
        UsbLink u = sUsb;
        if (u != null && !u.dead) {
            u.owner = this;
            link = u;
            onLinkUp();
            return;
        }
        UsbManager usb = (UsbManager) getSystemService(Context.USB_SERVICE);
        if (usb == null) {
            return;
        }
        UsbAccessory acc = attached;
        if (acc == null) {
            UsbAccessory[] list = usb.getAccessoryList();
            if (list != null && list.length > 0) {
                acc = list[0];
            }
        }
        if (acc == null) {
            return;
        }
        if (!usb.hasPermission(acc)) {
            requestUsbPermission(usb, acc);
            return;
        }
        ParcelFileDescriptor pfd;
        try {
            pfd = usb.openAccessory(acc);
        } catch (RuntimeException e) {
            Log.w(TAG, "openAccessory failed", e);
            pfd = null;
        }
        if (pfd == null) {
            show(State.IDLE, getString(R.string.usb_open_failed));
            return;
        }
        UsbLink opened = new UsbLink(pfd, acc.getDescription());
        sUsb = opened;
        opened.owner = this;
        opened.start();
        link = opened;
        onLinkUp();
    }

    private void requestUsbPermission(UsbManager usb, UsbAccessory acc) {
        if (permissionReceiver == null) {
            permissionReceiver = new BroadcastReceiver() {
                @Override
                public void onReceive(Context context, Intent intent) {
                    if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)) {
                        adoptUsb((UsbAccessory) intent.getParcelableExtra(UsbManager.EXTRA_ACCESSORY));
                    }
                }
            };
            IntentFilter f = new IntentFilter(ACTION_USB_PERMISSION);
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(permissionReceiver, f, Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(permissionReceiver, f);
            }
        }
        // The system fills in the result, so the PendingIntent must be mutable; on newer targets that
        // is only allowed for an explicit intent, hence setPackage.
        Intent intent = new Intent(ACTION_USB_PERMISSION).setPackage(getPackageName());
        int flags = Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0;
        usb.requestPermission(acc, PendingIntent.getBroadcast(this, 0, intent, flags));
    }

    // ------------------------------------------------------------------ messages

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
            } else if ("callback".equals(type)) {
                deliverCallback(m.optString("url"));
            } else if ("ping".equals(type)) {
                send("{\"type\":\"pong\"}");
            }
        } catch (JSONException e) {
            Log.w(TAG, "bad message", e);
        }
    }

    /**
     * The computer returns the address the browser ended up on. Hand it to the app that asked, but
     * only if it really is the return address of the page we're waiting on, with the same state, so
     * nothing else that can reach this connection can feed an app a login result.
     */
    private void deliverCallback(final String url) {
        String page = pendingUrl;
        if (page == null || url.isEmpty()) {
            send(errorJson("No sign-in is waiting for a result."));
            return;
        }
        int verdict = new Redirect.Request(page).accepts(url);
        if (verdict == Redirect.STATE_MISMATCH) {
            send(errorJson("That result is from a different sign-in."));
            return;
        }
        if (verdict != Redirect.OK) {
            send(errorJson("That isn't this sign-in's return address."));
            return;
        }
        final String caller = callerPackage();
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    if (caller != null) {
                        i.setPackage(caller);
                    }
                    startActivity(i);
                } catch (ActivityNotFoundException e) {
                    Log.w(TAG, "no activity for the callback", e);
                    send(errorJson("The app didn't accept the result."));
                    return;
                } catch (SecurityException e) {
                    Log.w(TAG, "callback refused", e);
                    send(errorJson("The app didn't accept the result."));
                    return;
                }
                pendingUrl = null;
                send("{\"type\":\"delivered\"}");
                // Let the app's own screen take the result before this one closes.
                main.postDelayed(new Runnable() {
                    @Override
                    public void run() {
                        finish();
                    }
                }, FINISH_DELAY_MS);
            }
        });
    }

    private static String errorJson(String text) {
        try {
            return new JSONObject().put("type", "error").put("text", text).toString();
        } catch (JSONException e) {
            return "{\"type\":\"error\"}";
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
}
