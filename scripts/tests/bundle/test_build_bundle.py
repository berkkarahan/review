#!/usr/bin/env python3
"""Regression tests for scripts/build_bundle.py (offline temp git repo). Run: python3 -I -B test_build_bundle.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "build_bundle.py"))
spec = importlib.util.spec_from_file_location("build_bundle", SCRIPT)
bb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bb)


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return p.stdout.strip()


def write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)


def run_tests():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    # 1. Parameter sanitization
    for bad in ("https://github.com/a/b/pull/1;rm -rf /", "a/b#1$(whoami)", "a/b#1`id`"):
        try:
            bb.parse_target(bad, ".")
            chk(f"sanitize rejects {bad}", False, "did not raise")
        except ValueError:
            chk(f"sanitize rejects {bad}", True)

    m_mode, o, r, n, url = bb.parse_target("https://github.com/octo/demo/pull/42", ".")
    chk("parse PR URL", (m_mode, o, r, n) == ("pr", "octo", "demo", "42"), (m_mode, o, r, n))

    # 2. Local branch diff mode with signals, slicing, micro-lane folding, denylist, rule base copy, and specs
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "repo")
        bundle = os.path.join(td, "bundle")
        os.makedirs(repo)
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")

        write(repo, "CLAUDE.md", "# Base rules\nAlways check tenant scope.\n")
        write(repo, "app/auth.py", "def check_auth(user):\n    return bool(user)\n")
        write(repo, "app/cache.py", "def load_item(k):\n    return k\n")
        write(repo, "app/async_pipeline.py", "async def run_job(x):\n    return x\n")
        write(repo, "docs/guide.md", "# Guide\nInitial docs.\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "base commit")

        git(repo, "checkout", "-b", "feat/demo")
        write(repo, "CLAUDE.md", "# Base rules\nAlways check tenant scope.\nNew rule line.\n")
        write(repo, "app/auth.py", "def check_auth(user, token):\n    # verify jwt permission\n    return bool(user and token)\n")
        # Small perf edit (<=40 lines, 1 file -> should micro-fold!)
        write(repo, "app/cache.py", "def load_item(k, redis_cache):\n    return redis_cache.get(k)\n")
        # Large async edit (>40 lines -> should stay its own robustness lane!)
        big_async = "async def run_job(x):\n" + "".join(f"    v{i} = await step({i})\n" for i in range(45)) + "    return v0\n"
        write(repo, "app/async_pipeline.py", big_async)
        write(repo, ".env", "SECRET_KEY=supersecret\n")
        write(repo, "docs/specs/auth-plan.md", "# Auth Plan\nAC1: Require token on auth.\n")
        write(repo, "docs/guide.md", "# Guide\nUpdated docs mentioning PROJ-123.\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "feat: update auth and worker for PROJ-123")

        p = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", bundle, "--effort", "high", "--local-only"],
            capture_output=True, text=True,
        )
        chk("CLI exits 0 in local-only mode", p.returncode == 0, p.stderr)
        summary = json.loads(p.stdout)

        chk("bundle-summary.json written", os.path.isfile(os.path.join(bundle, "bundle-summary.json")))
        chk("diff-map.json written", os.path.isfile(os.path.join(bundle, "diff-map.json")))
        chk("existing-comments.json is valid JSON object",
            set(json.load(open(os.path.join(bundle, "existing-comments.json"))).keys()) == {"inline", "reviews"})
        chk("signals detect auth, perf, async",
            {"auth", "perf", "async"}.issubset(set(summary["signals"].keys())), summary["signals"])
        chk("secret .env excluded from head/", not os.path.exists(os.path.join(bundle, "head", ".env")))
        chk("base/CLAUDE.md copied from base commit",
            os.path.isfile(os.path.join(bundle, "base", "CLAUDE.md"))
            and "New rule line." not in open(os.path.join(bundle, "base", "CLAUDE.md")).read())
        chk("intent_hints discovers diff spec and ticket PROJ-123",
            "docs/specs/auth-plan.md" in summary["intent_hints"]["diff_specs"]
            and "PROJ-123" in summary["intent_hints"]["tickets"], summary["intent_hints"])

        # Per-lane diff slicing verification
        sec_patch = open(os.path.join(bundle, "lanes", "security.patch")).read()
        chk("security slice includes auth.py and excludes docs/guide.md",
            "app/auth.py" in sec_patch and "docs/guide.md" not in sec_patch)
        rob_patch = open(os.path.join(bundle, "lanes", "robustness.patch")).read()
        chk("robustness slice includes async_pipeline.py and excludes auth.py",
            "app/async_pipeline.py" in rob_patch and "app/auth.py" not in rob_patch)

        # Micro-lane folding verification: performance (tiny) folded, robustness (45 lines) kept, security kept
        selected_names = [x["lane"] for x in summary["lane_plan"]["selected"]]
        folded_names = [x["lane"] for x in summary["lane_plan"]["folded"]]
        chk("micro-lane auto-fold folds tiny performance lane", "performance" in folded_names and "performance" not in selected_names, summary["lane_plan"])
        chk("large robustness lane stays separate", "robustness" in selected_names, summary["lane_plan"])
        chk("security lane stays separate on auth signal", "security" in selected_names, summary["lane_plan"])
        chk("high effort uses standard tier for all lanes",
            all(x["tier"] == "standard" for x in summary["lane_plan"]["selected"]), summary["lane_plan"]["selected"])

        # Model tiering check: fast tier ONLY on skim and low
        bundle_skim = os.path.join(td, "bundle-skim")
        p_sk = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", bundle_skim, "--effort", "skim", "--local-only"],
            capture_output=True, text=True,
        )
        s_sk = json.loads(p_sk.stdout)
        chk("skim effort assigns fast tier",
            s_sk["lane_plan"]["selected"] == [{"lane": "skim", "tier": "fast", "why": "skim profile"}], s_sk["lane_plan"])

        p_sk_nt = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", bundle_skim, "--effort", "skim", "--no-tier", "--local-only"],
            capture_output=True, text=True,
        )
        s_sk_nt = json.loads(p_sk_nt.stdout)
        chk("--no-tier overrides skim to standard tier",
            s_sk_nt["lane_plan"]["selected"][0]["tier"] == "standard", s_sk_nt["lane_plan"])

        # Low effort with >= 400 lines assigns fast tier to critical-risk
        write(repo, "app/large_mod.py", "".join(f"def fn_{i}(x):\n    return x + {i}\n" for i in range(210)))
        git(repo, "add", "app/large_mod.py")
        git(repo, "commit", "-m", "add large module for low effort test")
        bundle_low = os.path.join(td, "bundle-low")
        p_low = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", bundle_low, "--effort", "low", "--local-only"],
            capture_output=True, text=True,
        )
        s_low = json.loads(p_low.stdout)
        chk("low effort >=400 lines assigns fast tier to critical-risk",
            s_low["lane_plan"]["selected"] == [{"lane": "critical-risk", "tier": "fast", "why": "low effort single lane"}],
            s_low["lane_plan"])

    # 3. Docs-only diff skips code-quality and conventions
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "docs-repo")
        bundle = os.path.join(td, "docs-bundle")
        os.makedirs(repo)
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")
        write(repo, "README.md", "# Docs\nLine 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "base")
        git(repo, "checkout", "-b", "docs/update")
        write(repo, "README.md", "# Docs\n" + "".join(f"Paragraph {i}\n" for i in range(120)))
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "docs: expand readme")

        p = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", bundle, "--effort", "high", "--local-only"],
            capture_output=True, text=True,
        )
        s_docs = json.loads(p.stdout)
        sel = [x["lane"] for x in s_docs["lane_plan"]["selected"]]
        chk("docs-only diff skips code-quality and conventions",
            sel == ["adherence"], s_docs["lane_plan"])

    # 4. CODEBASE level tests: default stays PR, kwarg/flag parsing, file-map, all 7 lanes, scope path, sharding, rejections
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "cb-repo")
        os.makedirs(repo)
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")
        write(repo, "README.md", "# Demo Service\nProvides auth and billing APIs.\n")
        write(repo, "CLAUDE.md", "# Repo Rules\nAlways validate tenant IDs.\n")
        write(repo, "docs/specs/arch.md", "# Architecture\nAC1: Use parameterized SQL.\n")
        write(repo, ".env", "API_SECRET=do-not-read\n")
        write(repo, "src/auth.py", "def verify_jwt(token):\n    return bool(token)\n")
        write(repo, "src/db.py", "def run_query(conn, sql):\n    return conn.execute(sql)\n")
        write(repo, "src/worker.py", "async def process_job(payload):\n    return await handle(payload)\n")
        write(repo, "pkg/routes.py", "def v1_router(req):\n    return {'ok': True}\n")
        write(repo, "tests/test_auth.py", "def test_ok():\n    assert True\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "initial codebase")
        # Second commit so local-only PR default also works cleanly
        write(repo, "src/auth.py", "def verify_jwt(token):\n    return token is not None\n")
        git(repo, "add", "src/auth.py")
        git(repo, "commit", "-m", "tweak auth")

        # Default level without flag MUST be PR
        b_def = os.path.join(td, "b-default")
        p_def = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_def, "--local-only"],
            capture_output=True, text=True,
        )
        s_def = json.loads(p_def.stdout)
        chk("default level is always PR unless explicitly set",
            p_def.returncode == 0 and s_def["level"] == "PR" and s_def["target_mode"] == "local_branch", s_def)

        # Bare kwarg `level=CODEBASE` works
        b_cb = os.path.join(td, "b-cb")
        p_cb = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "level=CODEBASE", "--cwd", repo, "--bundle-dir", b_cb],
            capture_output=True, text=True,
        )
        chk("CODEBASE kwarg level=CODEBASE exits 0", p_cb.returncode == 0, p_cb.stderr)
        s_cb = json.loads(p_cb.stdout)
        chk("CODEBASE sets level=CODEBASE, target_mode=codebase, effort=high",
            s_cb["level"] == "CODEBASE" and s_cb["target_mode"] == "codebase" and s_cb["effort"]["resolved"] == "high",
            s_cb["effort"])

        fmap = json.load(open(os.path.join(b_cb, "file-map.json")))
        chk("CODEBASE file-map.json tracks repo files and excludes .env",
            "src/auth.py" in fmap and "pkg/routes.py" in fmap and ".env" not in fmap and fmap["src/auth.py"]["lines"] == 2,
            fmap)
        chk("CODEBASE discovers CLAUDE.md in rules-discovered.txt",
            "CLAUDE.md" in s_cb["rules_discovered"] and os.path.isfile(os.path.join(b_cb, "rules-discovered.txt")),
            s_cb["rules_discovered"])
        sel_cb = [x["lane"] for x in s_cb["lane_plan"]["selected"]]
        expected_7 = {"adherence", "code-quality", "conventions", "security", "performance", "compatibility", "robustness"}
        chk("CODEBASE auto->high selects all 7 lanes with no folding",
            set(sel_cb) == expected_7 and not s_cb["lane_plan"]["folded"], s_cb["lane_plan"])
        chk("CODEBASE writes per-lane .files.txt lists",
            all(os.path.isfile(os.path.join(b_cb, "lanes", f"{ln}.files.txt")) for ln in expected_7))

        # Subtree path scoping
        b_sub = os.path.join(td, "b-sub")
        p_sub = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "src/", "--level=CODEBASE", "--cwd", repo, "--bundle-dir", b_sub],
            capture_output=True, text=True,
        )
        s_sub = json.loads(p_sub.stdout)
        fmap_sub = json.load(open(os.path.join(b_sub, "file-map.json")))
        chk("CODEBASE scope path src/ limits file-map to src/",
            p_sub.returncode == 0 and s_sub["codebase"]["scope_path"] == "src"
            and set(fmap_sub.keys()) == {"src/auth.py", "src/db.py", "src/worker.py"}, fmap_sub)

        # Deterministic directory sharding when threshold exceeded
        b_sh = os.path.join(td, "b-shard")
        p_sh = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--level=CODEBASE", "--cwd", repo, "--bundle-dir", b_sh, "--shard-max-files", "2"],
            capture_output=True, text=True,
        )
        s_sh = json.loads(p_sh.stdout)
        cq_entries = [x for x in s_sh["lane_plan"]["selected"] if x["lane"] == "code-quality"]
        chk("CODEBASE shards large code-quality lane deterministically",
            p_sh.returncode == 0 and len(cq_entries) >= 2 and all("shard" in e for e in cq_entries)
            and all(os.path.isfile(os.path.join(b_sh, e["files_list"])) for e in cq_entries), cq_entries)

        # Reject --effort=skim and PR URLs at CODEBASE
        p_bad_eff = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--level=CODEBASE", "--effort=skim", "--cwd", repo, "--bundle-dir", b_cb],
            capture_output=True, text=True,
        )
        chk("CODEBASE rejects --effort=skim", p_bad_eff.returncode == 2 and "skim" in p_bad_eff.stderr, p_bad_eff.stderr)

        p_bad_tgt = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "https://github.com/octo/demo/pull/1", "--level=CODEBASE", "--cwd", repo, "--bundle-dir", b_cb],
            capture_output=True, text=True,
        )
        chk("CODEBASE rejects PR URL target", p_bad_tgt.returncode == 2 and "PR target" in p_bad_tgt.stderr, p_bad_tgt.stderr)

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
