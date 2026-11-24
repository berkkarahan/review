#!/usr/bin/env python3
"""Regression tests for scripts/precision.py. Run: python3 -I -B test_precision.py"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "precision.py"))


def load_precision_module():
    spec = importlib.util.spec_from_file_location("precision", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_tests():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    # 1. Import must have zero side effects (no sys.exit, no stdout, ignores sys.argv)
    old_argv = list(sys.argv)
    old_stdout = sys.stdout
    buf_imp = io.StringIO()
    sys.argv = ["test_precision.py", "/nonexistent/side_effect_path.jsonl"]
    sys.stdout = buf_imp
    try:
        pr = load_precision_module()
        imported_ok = buf_imp.getvalue() == ""
    except SystemExit:
        imported_ok = False
        pr = None
    finally:
        sys.argv = old_argv
        sys.stdout = old_stdout
    chk("import has zero side effects", imported_ok and pr is not None, buf_imp.getvalue())

    # 2. Weights, missing/null/empty rule normalization, malformed JSONL & non-dict JSON lines
    lines = [
        "",
        "   ",
        "{not valid json",
        "null",
        "123",
        '["a", "b"]',
        '"just a string"',
        json.dumps({"lane": "security", "severity": "HIGH", "rule": "injection", "decision": "unknown-dec"}),
        json.dumps({"lane": "security", "severity": "HIGH", "rule": None, "decision": "posted"}),
        json.dumps({"lane": "security", "severity": "HIGH", "decision": "chat-only"}),
        json.dumps({"lane": "code-quality", "severity": "MEDIUM", "rule": "", "decision": "dropped-by-user"}),
        json.dumps({"lane": "code-quality", "severity": "MEDIUM", "rule": "persona §8 #1", "decision": "dropped-by-eval"}),
    ]
    res = pr.compute_precision(lines)
    by_key = {g["key"]: g for g in res["groups"]}
    chk("compute_precision ignores malformed & non-dict lines and unknown decision", res["records"] == 4, res)
    chk("null/missing/empty rule maps to rule:none",
        by_key.get("rule:none", {}).get("n") == 3 and abs(by_key["rule:none"]["rate"] - 0.5) < 1e-9, by_key)
    chk("rule:None never emitted", "rule:None" not in by_key, by_key)
    chk("security keep rate is 75% ((1.0 + 0.5)/2)",
        by_key["lane:security"]["n"] == 2 and abs(by_key["lane:security"]["rate"] - 0.75) < 1e-9, by_key)

    # 3. n >= 10 ordering before n < 10 and deterministic tie-breaking
    many_lines = [
        json.dumps({"lane": "security", "severity": "HIGH", "rule": "r-high", "decision": "posted"})
        for _ in range(10)
    ] + [
        json.dumps({"lane": "conventions", "severity": "LOW", "rule": "r-small", "decision": "posted"})
    ]
    res_many = pr.compute_precision(many_lines)
    keys_order = [g["key"] for g in res_many["groups"]]
    chk("n>=10 groups sort ahead of n<10 groups deterministically",
        keys_order.index("lane:security") < keys_order.index("lane:conventions"), keys_order)

    # 4. CLI checks: valid JSONL, empty file, missing file, directory, --help, legacy fallback
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "feedback.jsonl")
        with open(fp, "w", encoding="utf-8") as fh:
            for ln in many_lines + [json.dumps({"lane": "conventions", "severity": "LOW", "rule": None, "decision": "dropped-by-user"})]:
                fh.write(ln + "\n")
        p = subprocess.run([sys.executable, "-I", "-B", SCRIPT, fp], capture_output=True, text=True)
        chk("CLI valid file exits 0 and prints keep rates without rule:None",
            p.returncode == 0 and "lane:security" in p.stdout and "rule:None" not in p.stdout and "rule:none" in p.stdout,
            (p.returncode, p.stdout, p.stderr))
        chk("CLI prints (n<10, not reliable) only for n<10",
            "(n<10, not reliable)" in p.stdout, p.stdout)

        empty_fp = os.path.join(td, "empty.jsonl")
        with open(empty_fp, "w", encoding="utf-8") as fh:
            fh.write("")
        p_empty = subprocess.run([sys.executable, "-I", "-B", SCRIPT, empty_fp], capture_output=True, text=True)
        chk("CLI empty file exits 0 with clear message",
            p_empty.returncode == 0 and "no valid feedback entries" in p_empty.stdout,
            (p_empty.returncode, p_empty.stdout, p_empty.stderr))

        invalid_only_fp = os.path.join(td, "invalid.jsonl")
        with open(invalid_only_fp, "w", encoding="utf-8") as fh:
            fh.write("{bad\nnull\n[]\n")
        p_inv = subprocess.run([sys.executable, "-I", "-B", SCRIPT, invalid_only_fp], capture_output=True, text=True)
        chk("CLI file with only invalid lines exits 0 with clear message",
            p_inv.returncode == 0 and "no valid feedback entries" in p_inv.stdout,
            (p_inv.returncode, p_inv.stdout, p_inv.stderr))

        p_miss = subprocess.run([sys.executable, "-I", "-B", SCRIPT, os.path.join(td, "missing.jsonl")], capture_output=True, text=True)
        chk("CLI missing file exits 1 with message on stderr",
            p_miss.returncode == 1 and "no feedback file" in p_miss.stderr,
            (p_miss.returncode, p_miss.stderr))

        p_dir = subprocess.run([sys.executable, "-I", "-B", SCRIPT, td], capture_output=True, text=True)
        chk("CLI directory path exits 2 cleanly",
            p_dir.returncode == 2 and "cannot read feedback file" in p_dir.stderr,
            (p_dir.returncode, p_dir.stderr))

        p_help = subprocess.run([sys.executable, "-I", "-B", SCRIPT, "--help"], capture_output=True, text=True)
        chk("CLI --help exits 0", p_help.returncode == 0 and "feedback.jsonl" in p_help.stdout, (p_help.returncode, p_help.stdout))

        # Legacy fallback resolution
        old_def, old_leg = pr.DEFAULT_PATH, pr.LEGACY_PATH
        try:
            pr.DEFAULT_PATH = os.path.join(td, "does_not_exist.jsonl")
            pr.LEGACY_PATH = fp
            chk("resolve_feedback_path falls back to legacy when default absent",
                pr.resolve_feedback_path() == fp, pr.resolve_feedback_path())
        finally:
            pr.DEFAULT_PATH, pr.LEGACY_PATH = old_def, old_leg

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
