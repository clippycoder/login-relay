// SPDX-License-Identifier: GPL-3.0-or-later
package io.github.clippycoder.oauthrelay;

import java.io.BufferedReader;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.util.Map;

/** Runs the shared test vectors through the phone's Redirect class. See run.sh. */
public final class RedirectTest {
    private static int failures = 0;
    private static int checks = 0;

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) {
            failures++;
            System.out.println("FAIL " + what);
        }
    }

    private static String nul(String s) {
        return s.equals("\\N") ? null : s;
    }

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(new InputStreamReader(new FileInputStream(args[0]), "UTF-8"));
        String line;
        while ((line = in.readLine()) != null) {
            String[] c = line.split("\t", -1);
            if (c[0].equals("accepts")) {
                int got = new Redirect.Request(c[1]).accepts(c[2]);
                String name = got == Redirect.OK ? "ok" : got == Redirect.UNRELATED ? "unrelated" : "stateMismatch";
                check(name.equals(c[3]), "accepts " + c[1] + " <- " + c[2] + ": expected " + c[3] + ", got " + name);
            } else if (c[0].equals("request")) {
                Redirect.Request r = new Redirect.Request(c[1]);
                if (!c[2].equals("\\?")) {
                    String want = nul(c[2]);
                    check(want == null ? r.redirect == null : want.equals(r.redirect), "redirect of " + c[1]);
                }
                if (!c[3].equals("\\?")) {
                    String want = nul(c[3]);
                    check(want == null ? r.state == null : want.equals(r.state), "state of " + c[1]);
                }
            } else if (c[0].equals("param")) {
                Map<String, String> p = Redirect.params(c[1]);
                check(c[3].equals(p.get(c[2])), "param " + c[2] + " of " + c[1] + ": got " + p.get(c[2]));
            } else if (c[0].equals("noparams")) {
                check(Redirect.params(c[1]).isEmpty(), "no params in " + c[1]);
            }
        }
        // A few phone-specific checks beyond the shared vectors.
        check(Redirect.same("COM.A.B://x/y/", "com.a.b://x/y"), "scheme case and trailing slash");
        check(!Redirect.same("com.a.b://x/y", "com.a.b://x/z"), "different path");
        check(new Redirect.Request("not a url").accepts("com.a://x") == Redirect.UNRELATED, "garbage page accepts nothing");
        System.out.println(failures == 0 ? "all " + checks + " checks passed" : failures + " of " + checks + " FAILED");
        System.exit(failures == 0 ? 0 : 1);
    }
}
