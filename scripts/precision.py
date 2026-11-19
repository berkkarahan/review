#!/usr/bin/env python3
"""Per-lane, per-severity and per-rule keep rate from feedback.jsonl.

Usage: precision.py [path]   (default ~/.claude/review/feedback.jsonl)
A finding is "kept" when decision == "posted". "chat-only" counts as half, because
the user saw it as useful but not worth posting. "dropped-by-user" and "dropped-by-eval" are 0.
Prints groups with at least 10 findings first. Stdlib only.
"""
import collections
import json
import os
import sys

default_path = os.path.expanduser("~/.claude/review/feedback.jsonl")
legacy_path = os.path.expanduser("~/.claude/generic-pr-review/feedback.jsonl")
if len(sys.argv) > 1:
    path = sys.argv[1]
elif not os.path.exists(default_path) and os.path.exists(legacy_path):
    path = legacy_path
else:
    path = default_path
WEIGHT = {"posted": 1.0, "chat-only": 0.5, "dropped-by-user": 0.0, "dropped-by-eval": 0.0}
groups = collections.defaultdict(lambda: [0, 0.0])
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    sys.exit(f"no feedback file at {path}")
for line in lines:
    try:
        r = json.loads(line)
    except ValueError:
        continue
    w = WEIGHT.get(r.get("decision"))
    if w is None:
        continue
    for key in (f"lane:{r.get('lane')}", f"severity:{r.get('severity')}", f"rule:{r.get('rule')}"):
        groups[key][0] += 1
        groups[key][1] += w
rows = sorted(groups.items(), key=lambda kv: (kv[1][0] < 10, kv[1][1] / kv[1][0]))
for key, (n, kept) in rows:
    flag = "" if n >= 10 else "  (n<10, not reliable)"
    print(f"{key:40} n={n:4}  keep={kept / n:5.0%}{flag}")
