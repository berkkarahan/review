#!/usr/bin/env python3
"""Regression tests for scripts/schema_lint.py (Gates G1-G4). Run: python3 -I -B test_schema_lint.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "schema_lint.py"))
spec = importlib.util.spec_from_file_location("schema_lint", SCRIPT)
schema_lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema_lint)

DMAP = {
    "a.py": {"added": [3, 4, 5], "context": [1, 2, 6]},
    "my dir/my file.py": {"added": [10, 11], "context": [9, 12]},
}


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

    # G1 invalid eval fields & non-dict finding
    act, rsn, _ = schema_lint.check_detail("not-a-dict", DMAP)
    chk("G1: non-dict finding -> retry_lane", act == "retry_lane", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(eval_disposition="ignore"), DMAP)
    chk("G1: invalid eval_disposition -> retry_lane", act == "retry_lane" and any("eval_disposition" in r for r in rsn), (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(eval_reason=""), DMAP)
    chk("G1: empty eval_reason -> retry_lane", act == "retry_lane" and any("eval_reason" in r for r in rsn), (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(eval_downgraded="yes"), DMAP)
    chk("G1: non-bool eval_downgraded -> retry_lane", act == "retry_lane" and any("eval_downgraded" in r for r in rsn), (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(locations=["a.py:3", 123]), DMAP)
    chk("G1: non-string in locations -> retry_lane", act == "retry_lane" and any("locations" in r for r in rsn), (act, rsn))

    # G1 invalid bare hunk and bare path formats
    act, rsn, _ = schema_lint.check_detail(base(location="@@ -1 +1 @@"), DMAP)
    chk("G1: bare hunk without path -> retry_lane", act == "retry_lane", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="a.py"), DMAP)
    chk("G1: bare path without line -> retry_lane", act == "retry_lane", (act, rsn))

    # Path normalization (./ and head/), paths with spaces, hunk headers, pure deletions, and huge ranges
    act, rsn, _ = schema_lint.check_detail(base(location="./head/a.py:3"), DMAP)
    chk("G2: ./head/ prefix stripped -> keep", act == "keep", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="my dir/my file.py:10"), DMAP)
    chk("G2: path with spaces LOC -> keep", act == "keep", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="my dir/my file.py @@ -8,2 +10,2 @@ def fn():"), DMAP)
    chk("G2: path with spaces HUNK_LOC -> keep", act == "keep", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="a.py @@ -1,2 +3,2 @@"), DMAP)
    chk("G2: path with hunk overlapping added lines -> keep", act == "keep", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="a.py @@ -5,2 +2,0 @@"), DMAP)
    chk("G2: pure-deletion hunk +2,0 matching context line 2 -> keep", act == "keep", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="a.py @@ -5,2 +99,0 @@"), DMAP)
    chk("G2: pure-deletion hunk +99,0 outside diff -> drop", act == "drop", (act, rsn))

    t0 = time.monotonic()
    act, rsn, _ = schema_lint.check_detail(base(location="a.py:1-1000000000"), DMAP)
    elapsed = time.monotonic() - t0
    chk("G2: huge line range completes fast without OOM", act == "keep" and elapsed < 0.5, (act, elapsed))

    act, rsn, _ = schema_lint.check_detail(base(location="a.py:20-10"), DMAP)
    chk("G2: reversed line range -> drop", act == "drop", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="a.py:0"), DMAP)
    chk("G2: line 0 -> drop", act == "drop", (act, rsn))

    # G2 drops out-of-diff and pre-existing findings
    act, rsn, _ = schema_lint.check_detail(base(location="a.py:99"), DMAP)
    chk("G2: out-of-diff line -> drop", act == "drop" and any("outside the PR diff" in r for r in rsn), (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(location="other.py:3"), DMAP)
    chk("G2: unchanged file -> drop", act == "drop", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(introduced_by_pr="pre-existing"), DMAP)
    chk("G2: introduced_by_pr pre-existing -> drop", act == "drop" and any("pre-existing" in r for r in rsn), (act, rsn))

    # G2 restricts INTENT_LOC to intent lanes & level-strictness, and validates locations[] alongside INTENT_LOC
    for lane in ("security", "code-quality"):
        act, rsn, _ = schema_lint.check_detail(base(lane=lane, location="PR description"), DMAP)
        chk(f"G2: {lane} with PR description -> drop", act == "drop", (act, rsn))

    for lane in ("adherence", "critical-risk", "skim"):
        for loc in ("PR description", "intent.md §AC1"):
            act, rsn, _ = schema_lint.check_detail(base(lane=lane, location=loc), DMAP)
            chk(f"G2: {lane} with {loc} -> keep", act == "keep", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(lane="adherence", location="Codebase architecture"), DMAP, level="PR")
    chk("G2: Codebase architecture rejected at PR level -> drop", act == "drop", (act, rsn))

    act, rsn, kept = schema_lint.check_detail(
        base(lane="adherence", location="intent.md §AC1", locations=["a.py:3", "a.py:99"]),
        DMAP,
        level="PR",
    )
    chk("G2: INTENT_LOC still prunes out-of-diff locations[]", act == "keep" and kept == ["intent.md §AC1", "a.py:3"], (act, rsn, kept))

    # G2 kept_locations pruning on mixed in-diff and out-of-diff sites
    act, rsn, kept = schema_lint.check_detail(base(location="a.py:99", locations=["a.py:4", "other.py:2"]), DMAP)
    chk("G2: mixed sites keep only in-diff kept_locations", act == "keep" and kept == ["a.py:4"], (act, rsn, kept))

    act, rsn, kept = schema_lint.check_detail(base(location="a.py:3", locations=["a.py:4"]), DMAP)
    chk("G2: all sites in-diff omit kept_locations", act == "keep" and kept is None, (act, rsn, kept))

    # G3 & G4
    act, rsn, _ = schema_lint.check_detail(base(severity="HIGH", trigger=""), DMAP)
    chk("G3: HIGH without trigger -> downgrade", act == "downgrade", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(lane="conventions", severity="HIGH", trigger="n/a (convention)"), DMAP)
    chk("G3: conventions HIGH with n/a (convention) -> keep", act == "keep", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(lane="code-quality", severity="HIGH", trigger="n/a (convention)"), DMAP)
    chk("G3: non-conventions HIGH with n/a (convention) -> downgrade", act == "downgrade", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(severity="LOW", confidence="LOW", trigger=""), DMAP)
    chk("G4: LOW with LOW confidence -> drop", act == "drop", (act, rsn))

    act, rsn, _ = schema_lint.check_detail(base(severity="QUESTION", confidence="LOW", trigger=""), DMAP)
    chk("G4: QUESTION with LOW confidence -> keep", act == "keep", (act, rsn))

    # CLI checks including dict wrapper and error exits
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

        fp_wrap = os.path.join(td, "wrap.json")
        with open(fp_wrap, "w", encoding="utf-8") as fh:
            json.dump({"findings": [base(location="a.py:3")]}, fh)
        p_wrap = subprocess.run([sys.executable, "-I", "-B", SCRIPT, fp_wrap, dp], capture_output=True, text=True)
        chk("CLI: accepts {'findings': [...]} wrapper",
            p_wrap.returncode == 0 and json.loads(p_wrap.stdout)["summary"] == {"keep": 1},
            (p_wrap.returncode, p_wrap.stderr))

        bad_json = os.path.join(td, "bad.json")
        with open(bad_json, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        p_bad = subprocess.run([sys.executable, "-I", "-B", SCRIPT, bad_json, dp], capture_output=True, text=True)
        chk("CLI: malformed JSON exits 2", p_bad.returncode == 2 and "schema_lint" in p_bad.stderr, (p_bad.returncode, p_bad.stderr))

        # CODEBASE level tests: file-map.json line bounds, int shorthand, 0-line files, strict INTENT_LOC
        fmap = {"a.py": {"lines": 25, "lang": "python"}, "b.py": 10, "empty.py": {"lines": 0}}
        act, rsn, kept = schema_lint.check_detail(base(location="a.py:20", introduced_by_pr="codebase"), fmap, level="CODEBASE")
        chk("CODEBASE G2: valid file line with introduced_by_pr=codebase -> keep", act == "keep" and not rsn and kept is None, (act, rsn))

        act, rsn, _ = schema_lint.check_detail(base(location="b.py:10"), fmap, level="CODEBASE")
        chk("CODEBASE G2: integer line-count shorthand accepted", act == "keep", (act, rsn))

        act, rsn, _ = schema_lint.check_detail(base(location="empty.py:1"), fmap, level="CODEBASE")
        chk("CODEBASE G2: 0-line file allows line 1", act == "keep", (act, rsn))

        act, rsn, _ = schema_lint.check_detail(base(location="empty.py:2"), fmap, level="CODEBASE")
        chk("CODEBASE G2: 0-line file drops line 2", act == "drop", (act, rsn))

        act, rsn, _ = schema_lint.check_detail(base(location="a.py:12", introduced_by_pr="pre-existing"), fmap, level="CODEBASE")
        chk("CODEBASE G2: pre-existing finding kept at CODEBASE level", act == "keep" and not rsn, (act, rsn))

        act, rsn, _ = schema_lint.check_detail(base(location="a.py:26"), fmap, level="CODEBASE")
        chk("CODEBASE G2: line past EOF -> drop", act == "drop" and any("codebase scope or line range" in r for r in rsn), (act, rsn))

        act, rsn, _ = schema_lint.check_detail(base(location="missing.py:1"), fmap, level="CODEBASE")
        chk("CODEBASE G2: unknown file -> drop", act == "drop", (act, rsn))

        act, rsn, kept = schema_lint.check_detail(base(location="a.py:99", locations=["b.py:5", "missing.py:1"]), fmap, level="CODEBASE")
        chk("CODEBASE G2: mixed locations keep only valid file-map sites", act == "keep" and kept == ["b.py:5"], (act, rsn, kept))

        act, rsn, _ = schema_lint.check_detail(base(lane="adherence", location="Codebase architecture"), fmap, level="CODEBASE")
        chk("CODEBASE G2: adherence allows Codebase architecture intent location", act == "keep", (act, rsn))

        act, rsn, _ = schema_lint.check_detail(base(lane="adherence", location="Codebase architecture §Auth"), fmap, level="CODEBASE")
        chk("CODEBASE G2: adherence allows Codebase architecture section", act == "keep", (act, rsn))

        act, rsn, _ = schema_lint.check_detail(base(lane="adherence", location="PR description"), fmap, level="CODEBASE")
        chk("CODEBASE G2: PR description rejected at CODEBASE level -> drop", act == "drop", (act, rsn))

        fp_cb = os.path.join(td, "f_cb.json")
        fm_cb = os.path.join(td, "fm_cb.json")
        with open(fp_cb, "w", encoding="utf-8") as fh:
            json.dump([base(id="cb-1", location="a.py:20", introduced_by_pr="codebase"), base(id="cb-2", location="a.py:99")], fh)
        with open(fm_cb, "w", encoding="utf-8") as fh:
            json.dump(fmap, fh)
        p_cb = subprocess.run([sys.executable, "-I", "-B", SCRIPT, fp_cb, fm_cb, "--level", "CODEBASE"], capture_output=True, text=True)
        out_cb = json.loads(p_cb.stdout)
        chk("CLI --level CODEBASE: keeps valid file line and drops out-of-range",
            p_cb.returncode == 0 and out_cb["summary"] == {"keep": 1, "drop": 1}, (p_cb.returncode, out_cb))

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
