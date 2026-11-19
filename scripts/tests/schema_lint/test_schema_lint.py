#!/usr/bin/env python3
"""Regression tests for scripts/schema_lint.py (Gates G1-G4). Run: python3 -I -B test_schema_lint.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "schema_lint.py"))
spec = importlib.util.spec_from_file_location("schema_lint", SCRIPT)
schema_lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema_lint)

DMAP = {"a.py": {"added": [3, 4, 5], "context": [1, 2, 6]}}


def base(lane="security", **kw):
    f = {
        "id": "f-1",
        "lane": lane,
        "severity": "HIGH",
        "confidence": "HIGH",
        "title": "Valid finding title",
        "location": "a.py:3",
        "issue": "Issue observed at added line.",
        "fix": "Fix the added line.",
        "trigger": "Input X causes wrong output Y.",
    }
    f.update(kw)
    return f


def run_tests():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    # G1 valid keep including new lanes and optional eval fields
    for lane in ("critical-risk", "evaluation"):
        act, rsn, kept = schema_lint.check_detail(
            base(lane=lane, eval_disposition="post", eval_reason="Actionable diff defect.", eval_downgraded=False),
            DMAP,
        )
        chk(f"G1: {lane} with eval fields kept", act == "keep" and not rsn and kept is None, (act, rsn))

    # G1 invalid eval fields
    act, rsn, _ = schema_lint.check_detail(base(eval_disposition="ignore"), DMAP)
    chk("G1: invalid eval_disposition -> retry_lane", act == "retry_lane" and any("eval_disposition" in r for r in rsn), (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(eval_reason=""), DMAP)
    chk("G1: empty eval_reason -> retry_lane", act == "retry_lane" and any("eval_reason" in r for r in rsn), (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(eval_downgraded="yes"), DMAP)
    chk("G1: non-bool eval_downgraded -> retry_lane", act == "retry_lane" and any("eval_downgraded" in r for r in rsn), (act, rsn))

    # G1 invalid bare hunk and bare path formats
    act, rsn, _ = schema_lint.check_detail(base(location="@@ -1 +1 @@"), DMAP)
    chk("G1: bare hunk without path -> retry_lane", act == "retry_lane", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="a.py"), DMAP)
    chk("G1: bare path without line -> retry_lane", act == "retry_lane", (act, rsn))

    # Path + hunk header valid when overlapping added lines
    act, rsn, _ = schema_lint.check_detail(base(location="a.py @@ -1,2 +3,2 @@"), DMAP)
    chk("G2: path with hunk overlapping added lines -> keep", act == "keep", (act, rsn))

    # G2 drops out-of-diff and pre-existing findings
    act, rsn, _ = schema_lint.check_detail(base(location="a.py:99"), DMAP)
    chk("G2: out-of-diff line -> drop", act == "drop" and any("outside the PR diff" in r for r in rsn), (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="other.py:3"), DMAP)
    chk("G2: unchanged file -> drop", act == "drop", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(introduced_by_pr="pre-existing"), DMAP)
    chk("G2: introduced_by_pr pre-existing -> drop", act == "drop" and any("pre-existing" in r for r in rsn), (act, rsn))

    # G2 restricts INTENT_LOC to intent lanes
    for lane in ("security", "code-quality"):
        act, rsn, _ = schema_lint.check_detail(base(lane=lane, location="PR description"), DMAP)
        chk(f"G2: {lane} with PR description -> drop", act == "drop", (act, rsn))

    for lane in ("adherence", "critical-risk", "skim"):
        for loc in ("PR description", "intent.md §AC1"):
            act, rsn, _ = schema_lint.check_detail(base(lane=lane, location=loc), DMAP)
            chk(f"G2: {lane} with {loc} -> keep", act == "keep", (act, rsn))

    # G2 kept_locations pruning on mixed in-diff and out-of-diff sites
    act, rsn, kept = schema_lint.check_detail(base(location="a.py:99", locations=["a.py:4", "other.py:2"]), DMAP)
    chk("G2: mixed sites keep only in-diff kept_locations", act == "keep" and kept == ["a.py:4"], (act, rsn, kept))

    act, rsn, kept = schema_lint.check_detail(base(location="a.py:3", locations=["a.py:4"]), DMAP)
    chk("G2: all sites in-diff omit kept_locations", act == "keep" and kept is None, (act, rsn, kept))

    # CLI check including kept_locations serialization
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "f.json")
        dp = os.path.join(td, "d.json")
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump([base(location="a.py:99", locations=["a.py:3"]), base(id="f-2", location="a.py:99")], fh)
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump(DMAP, fh)
        p = subprocess.run([sys.executable, "-I", "-B", SCRIPT, fp, dp], capture_output=True, text=True)
        out = json.loads(p.stdout)
        chk("CLI: exit 0 and kept_locations emitted",
            p.returncode == 0 and out["summary"] == {"keep": 1, "drop": 1}
            and out["results"][0].get("kept_locations") == ["a.py:3"]
            and "kept_locations" not in out["results"][1],
            (p.returncode, out))

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
