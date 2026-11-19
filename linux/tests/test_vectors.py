# SPDX-License-Identifier: GPL-3.0-or-later
"""Every section of tests/vectors.json, shared with the Swift and C# implementations."""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from loginrelay.redirect import RelayRequest, shell_quote, url_params, version_less  # noqa: E402

with open(os.path.join(HERE, "..", "..", "tests", "vectors.json"), encoding="utf-8") as f:
    VECTORS = json.load(f)


def snake(name):
    if name == "lacksPKCE":
        return "lacks_pkce"
    return re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), name)


class Vectors(unittest.TestCase):
    def test_requests(self):
        for v in VECTORS["requests"]:
            with self.subTest(v["name"]):
                r = RelayRequest(v["url"], v.get("referrer"), v.get("label"))
                for key, want in v["expect"].items():
                    self.assertEqual(getattr(r, snake(key)), want, key)

    def test_accepts(self):
        for v in VECTORS["accepts"]:
            with self.subTest(v["name"]):
                r = RelayRequest(v["url"], v.get("referrer"), v.get("label"))
                self.assertEqual(r.accepts(v["callback"]), v["expect"])

    def test_url_params(self):
        for v in VECTORS["urlParams"]:
            with self.subTest(v["name"]):
                self.assertEqual(url_params(v["url"]), v["expect"])

    def test_shell_quote(self):
        for raw, want in VECTORS["shellQuote"]:
            with self.subTest(raw):
                self.assertEqual(shell_quote(raw), want)

    def test_version_less(self):
        for a, b, want in VECTORS["versionLess"]:
            with self.subTest("%s < %s" % (a, b)):
                self.assertIs(version_less(a, b), want)

    def test_every_section_is_covered(self):
        sections = {k for k in VECTORS if not k.startswith("_")}
        self.assertEqual(sections, {"requests", "accepts", "urlParams", "shellQuote", "versionLess"})


if __name__ == "__main__":
    unittest.main()
