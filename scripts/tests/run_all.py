#!/usr/bin/env python3
"""Unified test runner for all _review script suites plus end-to-end cross-script integration checks.

Usage: python3 -I -B scripts/tests/run_all.py [--skip-e2e]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.normpath(os.path.join(HERE, ".."))

SUITES = [
    ("bundle", os.path.join(HERE, "bundle", "test_build_bundle.py")),
    ("diff_map", os.path.join(HERE, "bundle", "test_diff_map.py")),
    ("merge", os.path.join(HERE, "merge", "test_merge_findings.py")),
    ("schema_lint", os.path.join(HERE, "schema_lint", "test_schema_lint.py")),
    ("breaker", os.path.join(HERE, "breaker", "test_breaker.py")),
    ("carry", os.path.join(HERE, "carry", "run_tests.py")),
    ("ste_lint", os.path.join(HERE, "ste_lint", "test_ste_lint.py")),
    ("precision", os.path.join(HERE, "precision", "test_precision.py")),
]


def run_cmd(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)


def run_e2e_integration():
    """Verify build_bundle -> merge -> finalize --carry -> breaker -> carry -> ste_lint end-to-end."""
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    bb_py = os.path.join(SCRIPTS_DIR, "build_bundle.py")
    mf_py = os.path.join(SCRIPTS_DIR, "merge_findings.py")
    br_py = os.path.join(SCRIPTS_DIR, "breaker.py")
    cf_py = os.path.join(SCRIPTS_DIR, "carry_findings.py")
    sl_py = os.path.join(SCRIPTS_DIR, "ste_lint.py")

    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "demo_repo")
        reviews_dir = os.path.join(td, "reviews")
        os.makedirs(repo)
        os.makedirs(reviews_dir)

        def git(*args):
            p = run_cmd(["git", *args], cwd=repo)
            if p.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr}")
            return p.stdout.strip()

        git("init", "-b", "main")
        git("config", "user.email", "dev@example.com")
        git("config", "user.name", "Dev")
        write_file(os.path.join(repo, "app/auth.py"), "def check(user):\n    return bool(user)\n")
        write_file(os.path.join(repo, "app/calc.py"), "def total(x, y):\n    return x + y\n")
        git("add", "-A")
        git("commit", "-m", "base")

        git("checkout", "-b", "feat/check")
        write_file(
            os.path.join(repo, "app/auth.py"),
            "def check(user, role):\n    if role == 'admin':\n        return True\n    return bool(user)\n",
        )
        write_file(
            os.path.join(repo, "app/calc.py"),
            "def total(x, y):\n    if x < 0:\n        return 0\n    return x + y\n",
        )
        git("add", "-A")
        git("commit", "-m", "feat: add admin bypass and clamp")
        sha1 = git("rev-parse", "HEAD")

        # Round 1 PR pipeline
        b1 = os.path.join(td, "bundle1")
        p_b1 = run_cmd([
            sys.executable, "-I", "-B", bb_py,
            "--cwd", repo, "--bundle-dir", b1, "--effort", "high",
            "--local-only", "--reviews-dir", reviews_dir,
        ])
        chk("e2e PR Round 1 build_bundle exits 0", p_b1.returncode == 0, p_b1.stderr)
        dmap1 = json.load(open(os.path.join(b1, "diff-map.json"), encoding="utf-8"))
        auth_line = dmap1["app/auth.py"]["added"][0]
        calc_line = dmap1["app/calc.py"]["added"][0]

        write_json(os.path.join(b1, "raw-lanes.json"), {
            "security": "```json\n" + json.dumps({
                "lane": "security",
                "findings": [{
                    "id": "sec-1", "lane": "security", "severity": "HIGH", "confidence": "HIGH",
                    "title": "Admin bypass skips user check",
                    "location": f"app/auth.py:{auth_line}",
                    "issue": "Anyone passing admin role bypasses user validation.",
                    "trigger": "Call check(None, 'admin') and it returns True.",
                    "fix": "Require an authenticated user before the role check.",
                    "introduced_by_pr": "yes",
                }],
            }) + "\n```",
            "code-quality": {
                "lane": "code-quality",
                "findings": [{
                    "id": "cq-1", "lane": "code-quality", "severity": "MEDIUM", "confidence": "HIGH",
                    "title": "Negative input silently returns zero",
                    "location": f"app/calc.py:{calc_line}",
                    "issue": "Clamping negative values hides invalid caller arguments.",
                    "trigger": "Pass negative x to total.",
                    "fix": "Raise a value error on negative input.",
                    "introduced_by_pr": "yes",
                }],
            },
            "adherence": {"lane": "adherence", "findings": [], "ac_matrix": []},
        })

        p_m1 = run_cmd([sys.executable, "-I", "-B", mf_py, "merge", b1])
        chk("e2e PR Round 1 merge exits 0", p_m1.returncode == 0, p_m1.stderr)

        write_json(os.path.join(b1, "verifier-results.json"), [
            {"id": "sec-1", "verdict": "confirmed", "reason": "Confirmed bypass."},
        ])
        write_json(os.path.join(b1, "eval-results.json"), {
            "evaluations": [{"id": "cq-1", "disposition": "post", "eval_reason": "Valid defect."}],
        })
        p_f1 = run_cmd([sys.executable, "-I", "-B", mf_py, "finalize", b1, "--carry"])
        chk("e2e PR Round 1 finalize --carry exits 0", p_f1.returncode == 0, p_f1.stderr)
        f1_out = json.loads(p_f1.stdout)
        chk("e2e PR Round 1 verdict is APPROVE WITH COMMENTS",
            f1_out.get("valid") and f1_out.get("verdict") == "APPROVE WITH COMMENTS", f1_out)

        p_br1 = run_cmd([sys.executable, "-I", "-B", br_py, os.path.join(b1, "breaker-input.json")])
        chk("e2e PR Round 1 breaker standalone replay matches",
            p_br1.returncode == 0 and json.loads(p_br1.stdout).get("verdict") == f1_out.get("verdict"),
            (p_br1.returncode, p_br1.stderr))

        report1 = os.path.join(b1, "report.md")
        write_file(report1, "\n".join([
            "## Ready for review",
            "",
            "The diff introduces one high risk check and one medium edge case.",
            "",
            f"**S1 · Admin bypass skips user check** — `app/auth.py:{auth_line}`",
            "",
            "The function returns true for any admin role without a valid user.",
            "Fix: Require an authenticated user before the role check.",
            "",
            f"**D1 · Negative input silently returns zero** — `app/calc.py:{calc_line}`",
            "",
            "The helper clamps negative arguments instead of an explicit error.",
            "Fix: Raise a value error on negative input.",
            "",
        ]))
        p_sl1 = run_cmd([
            sys.executable, "-I", "-B", sl_py, report1, "--findings", os.path.join(b1, "reported.json"),
        ])
        chk("e2e PR Round 1 ste_lint passes on report + reported.json",
            p_sl1.returncode == 0, (p_sl1.stdout, p_sl1.stderr))

        # Archive Round 1 findings, mark D1 declined, push Commit 2 touching app/calc.py only
        archive1 = os.path.join(reviews_dir, f"local-demo_repo-0-{sha1[:7]}")
        os.makedirs(archive1, exist_ok=True)
        shutil.copy2(os.path.join(b1, "findings.json"), os.path.join(archive1, "findings.json"))
        decisions_path = os.path.join(td, "decisions.json")
        write_json(decisions_path, [{"id": "D1", "status": "declined", "reason": "Intentional clamp for legacy callers."}])
        p_mk = run_cmd([sys.executable, "-I", "-B", cf_py, "mark", os.path.join(archive1, "findings.json"), decisions_path])
        chk("e2e carry_findings mark exits 0", p_mk.returncode == 0, p_mk.stderr)

        write_file(
            os.path.join(repo, "app/calc.py"),
            "def total(x, y):\n    if x < 0:\n        return 0\n    return x + y + 0\n",
        )
        git("add", "app/calc.py")
        git("commit", "-m", "chore: touch calc only")

        b2 = os.path.join(td, "bundle2")
        p_b2 = run_cmd([
            sys.executable, "-I", "-B", bb_py,
            "--cwd", repo, "--bundle-dir", b2, "--effort", "high",
            "--local-only", "--reviews-dir", reviews_dir,
        ])
        chk("e2e PR Round 2 build_bundle loads carry state",
            p_b2.returncode == 0 and os.path.isfile(os.path.join(b2, "open.json"))
            and os.path.isfile(os.path.join(b2, "previous.json")),
            p_b2.stderr)

        # Re-raise S1 (outside delta diff app/calc.py!) via open.json exemption
        write_json(os.path.join(b2, "raw-lanes.json"), {
            "security": {
                "lane": "security",
                "findings": [{
                    "id": "S1", "lane": "security", "severity": "HIGH", "confidence": "HIGH",
                    "title": "Admin bypass skips user check",
                    "location": f"app/auth.py:{auth_line}",
                    "issue": "Anyone passing admin role bypasses user validation.",
                    "trigger": "Call check(None, 'admin') and it returns True.",
                    "fix": "Require an authenticated user before the role check.",
                    "introduced_by_pr": "yes",
                }],
            },
            "adherence": {"lane": "adherence", "findings": [], "ac_matrix": []},
        })
        p_m2 = run_cmd([sys.executable, "-I", "-B", mf_py, "merge", b2])
        m2_out = json.loads(p_m2.stdout)
        chk("e2e PR Round 2 merge keeps open.json S1 outside delta diff",
            p_m2.returncode == 0 and m2_out.get("merged_count") == 1, (p_m2.returncode, m2_out, p_m2.stderr))

        write_json(os.path.join(b2, "verifier-results.json"), [
            {"id": "S1", "verdict": "confirmed", "reason": "Still present."},
        ])
        p_f2 = run_cmd([sys.executable, "-I", "-B", mf_py, "finalize", b2, "--carry"])
        f2_out = json.loads(p_f2.stdout)
        chk("e2e PR Round 2 finalize --carry keeps S1 and passes breaker",
            p_f2.returncode == 0 and f2_out.get("valid") and f2_out.get("open", {}).get("high") == 1,
            (p_f2.returncode, f2_out, p_f2.stderr))

        # CODEBASE sharded pipeline
        b_cb = os.path.join(td, "bundle_cb")
        p_bcb = run_cmd([
            sys.executable, "-I", "-B", bb_py,
            "--level=CODEBASE", "--cwd", repo, "--bundle-dir", b_cb, "--shard-max-files", "1",
        ])
        chk("e2e CODEBASE build_bundle with sharding exits 0",
            p_bcb.returncode == 0 and os.path.isfile(os.path.join(b_cb, "file-map.json")), p_bcb.stderr)

        write_json(os.path.join(b_cb, "raw-lanes.json"), [
            {
                "lane": "code-quality",
                "shard": "code-quality-shard-1",
                "findings": [{
                    "id": "code-quality-1", "lane": "code-quality", "severity": "MEDIUM", "confidence": "HIGH",
                    "title": "Admin check bypass",
                    "location": "app/auth.py:2",
                    "issue": "Role branch skips user check.",
                    "fix": "Validate user before role check.",
                    "introduced_by_pr": "codebase",
                }],
            },
            {
                "lane": "code-quality",
                "shard": "code-quality-shard-2",
                "findings": [{
                    "id": "code-quality-1", "lane": "code-quality", "severity": "MEDIUM", "confidence": "HIGH",
                    "title": "Silent clamp",
                    "location": "app/calc.py:2",
                    "issue": "Negative argument returns zero.",
                    "fix": "Raise an error for negative inputs.",
                    "introduced_by_pr": "codebase",
                }],
            },
            {"lane": "adherence", "findings": [], "ac_matrix": []},
        ])
        p_mcb = run_cmd([sys.executable, "-I", "-B", mf_py, "merge", b_cb, "--level=CODEBASE"])
        mcb_out = json.loads(p_mcb.stdout)
        chk("e2e CODEBASE merge deduplicates/disambiguates colliding shard IDs",
            p_mcb.returncode == 0 and mcb_out.get("merged_count") == 2, (p_mcb.returncode, mcb_out, p_mcb.stderr))

        pre_cb = json.load(open(os.path.join(b_cb, "merged-pre-verify.json"), encoding="utf-8"))
        write_json(os.path.join(b_cb, "eval-results.json"), {
            "evaluations": [
                {"id": f["id"], "disposition": "post", "eval_reason": "Valid improvement."}
                for f in pre_cb["findings"]
            ],
        })
        p_fcb = run_cmd([sys.executable, "-I", "-B", mf_py, "finalize", b_cb, "--level=CODEBASE", "--carry"])
        fcb_out = json.loads(p_fcb.stdout)
        chk("e2e CODEBASE finalize --carry returns IMPROVEMENTS RECOMMENDED with event NONE",
            p_fcb.returncode == 0 and fcb_out.get("valid")
            and fcb_out.get("verdict") == "IMPROVEMENTS RECOMMENDED" and fcb_out.get("event") == "NONE",
            (p_fcb.returncode, fcb_out, p_fcb.stderr))

        p_brcb = run_cmd([sys.executable, "-I", "-B", br_py, os.path.join(b_cb, "breaker-input.json")])
        chk("e2e CODEBASE breaker standalone replay matches",
            p_brcb.returncode == 0 and json.loads(p_brcb.stdout).get("verdict") == "IMPROVEMENTS RECOMMENDED",
            (p_brcb.returncode, p_brcb.stderr))

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"E2E integration: {len(checks) - fails}/{len(checks)} passed\n")
    return fails == 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="run_all.py", description=__doc__.splitlines()[0])
    ap.add_argument("--skip-e2e", action="store_true", help="Skip cross-script E2E integration tests")
    args = ap.parse_args(argv)

    failed_suites = []
    ran = 0
    for name, path in SUITES:
        if not os.path.isfile(path):
            print(f"SKIP {name}: {path} not found")
            continue
        ran += 1
        print(f"=== Running {name}: {os.path.relpath(path, SCRIPTS_DIR)} ===")
        p = run_cmd([sys.executable, "-I", "-B", path])
        if p.stdout:
            sys.stdout.write(p.stdout)
        if p.stderr:
            sys.stderr.write(p.stderr)
        if p.returncode != 0:
            failed_suites.append(name)
            print(f"SUITE FAILED: {name} (exit {p.returncode})\n")
        else:
            print(f"SUITE PASSED: {name}\n")

    e2e_ok = True
    if not args.skip_e2e:
        print("=== Running e2e_integration ===")
        e2e_ok = run_e2e_integration()
        if not e2e_ok:
            failed_suites.append("e2e_integration")

    total = ran + (0 if args.skip_e2e else 1)
    if failed_suites:
        print(f"SUMMARY: {total - len(failed_suites)}/{total} passed; failed: {', '.join(failed_suites)}")
        return 1
    print(f"SUMMARY: {total}/{total} suites passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
