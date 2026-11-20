# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure logic, no GUI: classifies a page the phone wants opened and decides what to wait for.
Direct port of macos/Sources/Redirect.swift; tests/vectors.json covers it."""
from __future__ import annotations

import re
from typing import Dict, Optional
from urllib.parse import unquote

SIGN_IN = "signIn"
SIGN_OUT = "signOut"
PLAIN_LINK = "plainLink"

OK = "ok"
UNRELATED = "unrelated"
STATE_MISMATCH = "stateMismatch"

_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _decode(s: str) -> str:
    # '+' is a space; undecodable input is kept as is (like Swift's removingPercentEncoding fallback).
    spaced = s.replace("+", " ")
    if _BAD_PERCENT.search(spaced):
        return spaced
    try:
        return unquote(spaced, errors="strict")
    except UnicodeDecodeError:
        return spaced


def url_params(url: str) -> Dict[str, str]:
    """Query and fragment parameters of a URL, percent-decoded; the first value wins."""
    result: Dict[str, str] = {}
    rest = url
    fragment = ""
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
    query = ""
    if "?" in rest:
        query = rest.split("?", 1)[1]
    for chunk in (query, fragment):
        for pair in chunk.split("&"):
            if not pair:
                continue
            kv = pair.split("=", 1)
            key = _decode(kv[0])
            value = _decode(kv[1]) if len(kv) > 1 else ""
            if key not in result:
                result[key] = value
    return result


def _base(url: str) -> str:
    """Everything before ? or # (the part that identifies a redirect target)."""
    end = len(url)
    for ch in "?#":
        i = url.find(ch)
        if i >= 0:
            end = min(end, i)
    return url[:end]


def same_redirect(callback: str, redirect: str) -> bool:
    """True if `callback` is the redirect URI plus an optional query or fragment. The scheme
    compares case-insensitively and a trailing slash doesn't matter."""

    def split(s: str):
        b = _base(s)
        colon = b.find(":")
        if colon < 0:
            return None
        return b[:colon].lower(), b[colon + 1:].rstrip("/")

    a, b = split(callback), split(redirect)
    if a is None or b is None:
        return False
    return a == b


def url_scheme(url: str) -> Optional[str]:
    colon = url.find(":")
    if colon < 0:
        return None
    s = url[:colon].lower()
    return s or None


def _host_of(url: str) -> Optional[str]:
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://([^/?#]*)", url)
    if not m:
        return None
    authority = m.group(1).rsplit("@", 1)[-1]
    if authority.startswith("["):
        end = authority.find("]")
        host = authority[1:end] if end > 0 else None
    else:
        host = authority.split(":", 1)[0]
    return host or None


class RelayRequest:
    """A page the phone wants opened."""

    def __init__(self, url: str, referrer: Optional[str] = None, label: Optional[str] = None):
        p = url_params(url)
        self.url = url
        self.referrer = referrer
        self.label = label if label else None
        self.caller: Optional[str] = None
        prefix = "android-app://"
        if referrer and referrer.startswith(prefix):
            parts = [x for x in referrer[len(prefix):].split("/") if x]
            self.caller = parts[0] if parts else None
        # Sign-in uses redirect_uri. Sign-out uses post_logout_redirect_uri (OpenID Connect) or
        # returnTo (Auth0's /v2/logout).
        self.redirect: Optional[str]
        if "redirect_uri" in p:
            self.kind = SIGN_IN
            self.redirect = p["redirect_uri"]
        elif "post_logout_redirect_uri" in p or "returnTo" in p:
            self.kind = SIGN_OUT
            self.redirect = p.get("post_logout_redirect_uri", p.get("returnTo"))
        else:
            self.kind = PLAIN_LINK
            self.redirect = None
        self.state = p.get("state")
        self.response_type = p.get("response_type", "code")
        self.has_pkce = "code_challenge" in p
        self.host = _host_of(url)

    @property
    def lacks_pkce(self) -> bool:
        """An authorization-code sign-in without PKCE hands a redeemable code through this computer."""
        return self.kind == SIGN_IN and "code" in self.response_type and not self.has_pkce

    @property
    def catchable_scheme(self) -> Optional[str]:
        """A custom scheme the computer can claim for one login. http(s) return addresses (App Links)
        can't be intercepted this way, so those fall back to pasting the final address."""
        if self.redirect is None:
            return None
        s = url_scheme(self.redirect)
        if s is None or s in ("http", "https"):
            return None
        return s

    @property
    def app_name(self) -> str:
        return self.label or self.caller or "An app"

    @property
    def verb(self) -> str:
        return {SIGN_IN: "sign-in", SIGN_OUT: "sign-out", PLAIN_LINK: "link"}[self.kind]

    def accepts(self, callback: str) -> str:
        """Does this address, arriving at the computer, belong to this request?"""
        if self.redirect is None:
            return UNRELATED
        if not same_redirect(callback, self.redirect):
            return UNRELATED
        if self.state is not None and url_params(callback).get("state") != self.state:
            return STATE_MISMATCH
        return OK
