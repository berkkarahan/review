#!/usr/bin/env python3
"""Per-lane, per-severity and per-rule keep rate from feedback.jsonl.

Usage: precision.py [path]   (default ~/.claude/review/feedback.jsonl)
A finding is "kept" when decision == "posted". "chat-only" counts as half, because
the user saw it as useful but not worth posting. "dropped-by-user" and "dropped-by-eval" are 0.
Prints groups with at least 10 findings first. Stdlib only.
"""
import argparse
import collections
import json
import os
import sys

DEFAULT_PATH = os.path.expanduser("~/.claude/review/feedback.jsonl")
LEGACY_PATH = os.path.expanduser("~/.claude/generic-pr-review/feedback.jsonl")
WEIGHT = {"posted": 1.0, "chat-only": 0.5, "dropped-by-user": 0.0, "dropped-by-eval": 0.0}


def resolve_feedback_path(path=None):
    """Resolve feedback JSONL path with legacy fallback when default does not exist."""
    if path:
        return path
    if not os.path.exists(DEFAULT_PATH) and os.path.exists(LEGACY_PATH):
        return LEGACY_PATH
    return DEFAULT_PATH


def _norm_dim(value, fallback):
    s = str(value).strip() if value is not None else ""
    return s or fallback


def compute_precision(lines):
    """Aggregate keep rates by lane, severity, and rule from JSONL lines."""
    groups = collections.defaultdict(lambda: [0, 0.0])
    records = 0
    for raw in lines:
        line = str(raw).strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        w = WEIGHT.get(row.get("decision"))
        if w is None:
            continue
        records += 1
        lane = _norm_dim(row.get("lane"), "unknown")
        severity = _norm_dim(row.get("severity"), "unknown")
        rule = _norm_dim(row.get("rule"), "none")
        for key in (f"lane:{lane}", f"severity:{severity}", f"rule:{rule}"):
            groups[key][0] += 1
            groups[key][1] += w

    sorted_rows = sorted(
        groups.items(),
        key=lambda kv: (kv[1][0] < 10, kv[1][1] / kv[1][0], kv[0]),
    )
    return {
        "records": records,
        "groups": [
            {"key": key, "n": n, "kept": kept, "rate": kept / n}
            for key, (n, kept) in sorted_rows
        ],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="precision.py", description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", help="Path to feedback.jsonl (default ~/.claude/review/feedback.jsonl)")
    args = ap.parse_args(argv)
    path = resolve_feedback_path(args.path)
    try:
        with open(path, encoding="utf-8") as fh:
            result = compute_precision(fh)
    except FileNotFoundError:
        sys.stderr.write(f"no feedback file at {path}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"cannot read feedback file {path}: {exc}\n")
        return 2

    if result["records"] == 0:
        print(f"no valid feedback entries in {path}")
        return 0

    for item in result["groups"]:
        flag = "" if item["n"] >= 10 else "  (n<10, not reliable)"
        print(f"{item['key']:40} n={item['n']:4}  keep={item['rate']:5.0%}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
