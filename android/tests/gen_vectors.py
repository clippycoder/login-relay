#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns tests/vectors.json into a tab-separated file the Java test can read without a JSON library.

    python3 gen_vectors.py OUT.tsv

Rows (tab-separated; \\N means "no value"):
    accepts   <page url>  <callback>   <ok|unrelated|stateMismatch>
    request   <page url>  <redirect>   <state>                   (only where the vector states them)
    param     <url>       <key>        <value>
    noparams  <url>
"""
import json
import os
import sys

NULL = "\\N"


def cell(v):
    return NULL if v is None else str(v)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "..", "tests", "vectors.json"), encoding="utf-8") as f:
        v = json.load(f)
    rows = []
    for c in v["accepts"]:
        rows.append(["accepts", c["url"], c["callback"], c["expect"]])
    for c in v["requests"]:
        e = c["expect"]
        if "redirect" in e or "state" in e:
            rows.append(["request", c["url"], cell(e.get("redirect")) if "redirect" in e else "\\?",
                         cell(e.get("state")) if "state" in e else "\\?"])
    for c in v["urlParams"]:
        if c["expect"]:
            for k, val in c["expect"].items():
                rows.append(["param", c["url"], k, val])
        else:
            rows.append(["noparams", c["url"]])
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        for r in rows:
            assert not any("\t" in x or "\n" in x for x in r), r
            f.write("\t".join(r) + "\n")
    print(f"{len(rows)} cases")


if __name__ == "__main__":
    main()
