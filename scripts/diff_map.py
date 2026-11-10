#!/usr/bin/env python3
"""Build a right-side line map from a unified diff.

Usage: diff_map.py <diff.patch> > diff-map.json
Output: {"<path>": {"added": [lines], "context": [lines]}}
Used by: gate G2 (in-diff check, Phase 2.2) and Posting step 2 (inline anchors).
Stdlib only. Never executes anything from the diff.
"""
import json
import re
import sys

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def build(text):
    out, path, new_line = {}, None, 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            path = None
        elif raw.startswith("+++ "):
            tgt = raw[4:].strip()
            path = None if tgt == "/dev/null" else (tgt[2:] if tgt.startswith("b/") else tgt)
            if path:
                out.setdefault(path, {"added": [], "context": []})
        elif raw.startswith("@@") and path:
            m = HUNK.match(raw)
            if m:
                new_line = int(m.group(1))
        elif path and new_line:
            if raw.startswith("+"):
                out[path]["added"].append(new_line)
                new_line += 1
            elif raw.startswith("-") or raw.startswith("\\"):
                continue
            else:
                out[path]["context"].append(new_line)
                new_line += 1
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: diff_map.py <diff.patch>")
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        json.dump(build(fh.read()), sys.stdout)
