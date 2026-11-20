// SPDX-License-Identifier: GPL-3.0-or-later
package io.github.clippycoder.oauthrelay;

import java.net.URLDecoder;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/**
 * Decides whether a "return address" belongs to the sign-in the phone is waiting on. A direct port of
 * the matching rules in macos/Sources/Redirect.swift (shared test cases: tests/vectors.json), so the
 * phone can check a callback itself before handing it to an app. Pure Java: no Android classes, so it
 * can be tested on a plain JVM (android/tests/run.sh).
 */
final class Redirect {
    private Redirect() {
    }

    static final int OK = 0;
    static final int UNRELATED = 1;
    static final int STATE_MISMATCH = 2;

    /** Query and fragment parameters of a URL, percent-decoded; the first value wins. */
    static Map<String, String> params(String url) {
        Map<String, String> out = new HashMap<String, String>();
        String rest = url;
        String fragment = "";
        int hash = rest.indexOf('#');
        if (hash >= 0) {
            fragment = rest.substring(hash + 1);
            rest = rest.substring(0, hash);
        }
        String query = "";
        int q = rest.indexOf('?');
        if (q >= 0) {
            query = rest.substring(q + 1);
        }
        for (String chunk : new String[] {query, fragment}) {
            for (String pair : chunk.split("&")) {
                if (pair.isEmpty()) {
                    continue;
                }
                int eq = pair.indexOf('=');
                String key = decode(eq < 0 ? pair : pair.substring(0, eq));
                String value = eq < 0 ? "" : decode(pair.substring(eq + 1));
                if (!out.containsKey(key)) {
                    out.put(key, value);
                }
            }
        }
        return out;
    }

    private static String decode(String s) {
        String spaced = s.replace('+', ' ');
        try {
            return URLDecoder.decode(spaced, "UTF-8");
        } catch (Exception e) {
            return spaced; // malformed percent escapes: keep the text as is
        }
    }

    /** Everything before ? or #. */
    private static String base(String url) {
        int end = url.length();
        int q = url.indexOf('?');
        if (q >= 0) {
            end = Math.min(end, q);
        }
        int h = url.indexOf('#');
        if (h >= 0) {
            end = Math.min(end, h);
        }
        return url.substring(0, end);
    }

    /**
     * True if callback is the redirect URI plus an optional query or fragment. The scheme compares
     * case-insensitively and trailing slashes don't matter.
     */
    static boolean same(String callback, String redirect) {
        String[] a = split(callback);
        String[] b = split(redirect);
        return a != null && b != null && a[0].equals(b[0]) && a[1].equals(b[1]);
    }

    private static String[] split(String url) {
        String b = base(url);
        int colon = b.indexOf(':');
        if (colon < 0) {
            return null;
        }
        String rest = b.substring(colon + 1);
        while (rest.endsWith("/")) {
            rest = rest.substring(0, rest.length() - 1);
        }
        return new String[] {b.substring(0, colon).toLowerCase(Locale.ROOT), rest};
    }

    /** The page the phone was asked to open, reduced to what's needed to vet its callback. */
    static final class Request {
        /** Where the browser will send the user back to the app; null for a plain link. */
        final String redirect;
        final String state;

        Request(String url) {
            Map<String, String> p = params(url);
            String r = p.get("redirect_uri");
            if (r == null) {
                // Sign-out: OpenID Connect uses post_logout_redirect_uri, Auth0's /v2/logout uses returnTo.
                r = p.get("post_logout_redirect_uri");
                if (r == null) {
                    r = p.get("returnTo");
                }
            }
            redirect = r;
            state = p.get("state");
        }

        /** OK, UNRELATED (not this page's return address) or STATE_MISMATCH. */
        int accepts(String callback) {
            if (redirect == null || !same(callback, redirect)) {
                return UNRELATED;
            }
            if (state != null && !state.equals(params(callback).get("state"))) {
                return STATE_MISMATCH;
            }
            return OK;
        }
    }
}
