// SPDX-License-Identifier: GPL-3.0-or-later
// Pure logic: classifies a page the phone wants opened and decides what to wait for.
// A direct port of macos/Sources/Redirect.swift; tests/vectors.json covers it.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;

namespace LoginRelay.Core
{
    public enum RequestKind
    {
        SignIn,
        SignOut,
        PlainLink,
    }

    public enum CallbackVerdict
    {
        Ok,
        Unrelated,
        StateMismatch,
    }

    public static class Redirect
    {
        /// <summary>Query and fragment parameters of a URL, percent-decoded; the first value wins.</summary>
        public static Dictionary<string, string> UrlParams(string url)
        {
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            string rest = url;
            string fragment = "";
            int hash = rest.IndexOf('#');
            if (hash >= 0)
            {
                fragment = rest.Substring(hash + 1);
                rest = rest.Substring(0, hash);
            }
            string query = "";
            int q = rest.IndexOf('?');
            if (q >= 0)
            {
                query = rest.Substring(q + 1);
            }
            foreach (string chunk in new[] { query, fragment })
            {
                foreach (string pair in chunk.Split('&', StringSplitOptions.RemoveEmptyEntries))
                {
                    int eq = pair.IndexOf('=');
                    string key = Decode(eq >= 0 ? pair.Substring(0, eq) : pair);
                    string value = eq >= 0 ? Decode(pair.Substring(eq + 1)) : "";
                    if (!result.ContainsKey(key))
                    {
                        result[key] = value;
                    }
                }
            }
            return result;
        }

        /// <summary>'+' is a space, then percent-decoding. Invalid escapes leave the text as it was (like Swift).</summary>
        private static string Decode(string s)
        {
            string spaced = s.Replace('+', ' ');
            if (spaced.IndexOf('%') < 0)
            {
                return spaced;
            }
            var strict = new UTF8Encoding(false, true);
            var sb = new StringBuilder();
            var pending = new List<byte>();
            try
            {
                int i = 0;
                while (i < spaced.Length)
                {
                    char c = spaced[i];
                    if (c == '%')
                    {
                        if (i + 2 >= spaced.Length || !Uri.IsHexDigit(spaced[i + 1]) || !Uri.IsHexDigit(spaced[i + 2]))
                        {
                            return spaced;
                        }
                        pending.Add(Convert.ToByte(spaced.Substring(i + 1, 2), 16));
                        i += 3;
                    }
                    else
                    {
                        if (pending.Count > 0)
                        {
                            sb.Append(strict.GetString(pending.ToArray()));
                            pending.Clear();
                        }
                        sb.Append(c);
                        i++;
                    }
                }
                if (pending.Count > 0)
                {
                    sb.Append(strict.GetString(pending.ToArray()));
                }
            }
            catch (DecoderFallbackException)
            {
                return spaced;
            }
            return sb.ToString();
        }

        /// <summary>Everything before ? or # (the part that identifies a redirect target).</summary>
        private static string BaseOf(string url)
        {
            int end = url.Length;
            int q = url.IndexOf('?');
            if (q >= 0 && q < end)
            {
                end = q;
            }
            int h = url.IndexOf('#');
            if (h >= 0 && h < end)
            {
                end = h;
            }
            return url.Substring(0, end);
        }

        private static bool TrySplit(string s, out string scheme, out string rest)
        {
            string b = BaseOf(s);
            int colon = b.IndexOf(':');
            if (colon < 0)
            {
                scheme = "";
                rest = "";
                return false;
            }
            scheme = b.Substring(0, colon).ToLowerInvariant();
            rest = b.Substring(colon + 1).TrimEnd('/');
            return true;
        }

        /// <summary>
        /// True if <paramref name="callback"/> is the redirect URI plus an optional query or fragment. The
        /// scheme compares case-insensitively and a trailing slash doesn't matter.
        /// </summary>
        public static bool SameRedirect(string callback, string redirect)
        {
            if (!TrySplit(callback, out string schemeA, out string restA))
            {
                return false;
            }
            if (!TrySplit(redirect, out string schemeB, out string restB))
            {
                return false;
            }
            return schemeA == schemeB && restA == restB;
        }

        /// <summary>Lower-cased scheme of an address, or null if it has none.</summary>
        public static string? UrlScheme(string url)
        {
            int colon = url.IndexOf(':');
            if (colon < 0)
            {
                return null;
            }
            string s = url.Substring(0, colon).ToLowerInvariant();
            return s.Length == 0 ? null : s;
        }
    }

    /// <summary>One page the phone asked to have opened.</summary>
    public sealed class RelayRequest
    {
        public string Url { get; }
        public string? Referrer { get; }

        /// <summary>Package name of the app that asked for the page, from "android-app://package".</summary>
        public string? Caller { get; }

        /// <summary>Display name the phone resolved for that app, if the phone app is new enough to send one.</summary>
        public string? Label { get; }

        public RequestKind Kind { get; }

        /// <summary>Where the browser will send the user back to the app.</summary>
        public string? RedirectAddress { get; }

        public string? State { get; }
        public string ResponseType { get; }
        public bool HasPkce { get; }
        public string? Host { get; }

        public RelayRequest(string url, string? referrer, string? label = null)
        {
            Dictionary<string, string> p = Redirect.UrlParams(url);
            Url = url;
            Referrer = referrer;
            Label = string.IsNullOrEmpty(label) ? null : label;

            const string prefix = "android-app://";
            if (referrer != null && referrer.StartsWith(prefix, StringComparison.Ordinal))
            {
                string[] parts = referrer.Substring(prefix.Length).Split('/', StringSplitOptions.RemoveEmptyEntries);
                Caller = parts.Length > 0 && parts[0].Length > 0 ? parts[0] : null;
            }
            else
            {
                Caller = null;
            }

            // Sign-in uses redirect_uri. Sign-out uses post_logout_redirect_uri (OpenID Connect) or
            // returnTo (Auth0's /v2/logout).
            string? found;
            if (p.TryGetValue("redirect_uri", out found))
            {
                Kind = RequestKind.SignIn;
                RedirectAddress = found;
            }
            else if (p.TryGetValue("post_logout_redirect_uri", out found) || p.TryGetValue("returnTo", out found))
            {
                Kind = RequestKind.SignOut;
                RedirectAddress = found;
            }
            else
            {
                Kind = RequestKind.PlainLink;
                RedirectAddress = null;
            }

            State = p.TryGetValue("state", out string? st) ? st : null;
            ResponseType = p.TryGetValue("response_type", out string? rt) ? rt : "code";
            HasPkce = p.ContainsKey("code_challenge");

            string? host = null;
            if (Uri.TryCreate(url, UriKind.Absolute, out Uri? parsed))
            {
                try
                {
                    host = parsed.Host;
                }
                catch (InvalidOperationException)
                {
                    host = null;
                }
                if (string.IsNullOrEmpty(host))
                {
                    host = null;
                }
            }
            Host = host;
        }

        /// <summary>An authorization-code sign-in without PKCE hands a redeemable code through this computer.</summary>
        public bool LacksPkce
        {
            get { return Kind == RequestKind.SignIn && ResponseType.Contains("code") && !HasPkce; }
        }

        /// <summary>
        /// A custom scheme the computer can claim for one login. http(s) return addresses (App Links) can't be
        /// intercepted this way, so those fall back to pasting the final address.
        /// </summary>
        public string? CatchableScheme
        {
            get
            {
                if (RedirectAddress == null)
                {
                    return null;
                }
                string? s = Redirect.UrlScheme(RedirectAddress);
                if (s == null || s == "http" || s == "https")
                {
                    return null;
                }
                return s;
            }
        }

        /// <summary>Short name for menus and notifications.</summary>
        public string AppName
        {
            get { return Label ?? Caller ?? "An app"; }
        }

        public string Verb
        {
            get
            {
                switch (Kind)
                {
                    case RequestKind.SignIn: return "sign-in";
                    case RequestKind.SignOut: return "sign-out";
                    default: return "link";
                }
            }
        }

        /// <summary>Does this address, arriving at the computer, belong to this request?</summary>
        public CallbackVerdict Accepts(string callback)
        {
            if (RedirectAddress == null)
            {
                return CallbackVerdict.Unrelated;
            }
            if (!Redirect.SameRedirect(callback, RedirectAddress))
            {
                return CallbackVerdict.Unrelated;
            }
            if (State != null)
            {
                string? got = Redirect.UrlParams(callback).TryGetValue("state", out string? s) ? s : null;
                if (got != State)
                {
                    return CallbackVerdict.StateMismatch;
                }
            }
            return CallbackVerdict.Ok;
        }
    }
}
