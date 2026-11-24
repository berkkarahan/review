#!/usr/bin/env python3
"""Regression tests for scripts/build_bundle.py (offline temp git repo). Run: python3 -I -B test_build_bundle.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

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

    # 1. Parameter sanitization & path safety
    for bad in (
        "https://github.com/a/b/pull/1;rm -rf /",
        "a/b#1$(whoami)",
        "a/b#1`id`",
        "https://github.com/../etc/pull/1",
        "a/..#1",
    ):
        try:
            bb.parse_target(bad, ".")
            chk(f"sanitize rejects {bad}", False, "did not raise")
        except ValueError:
            chk(f"sanitize rejects {bad}", True)

    m_mode, o, r, n, url = bb.parse_target("https://github.com/octo/demo/pull/42", ".")
    chk("parse PR URL", (m_mode, o, r, n) == ("pr", "octo", "demo", "42"), (m_mode, o, r, n))

    m_q = bb.parse_target("https://github.com/octo/demo/pull/42?w=1#discussion_r9", ".")
    chk("parse PR URL with query and fragment", m_q == ("pr", "octo", "demo", "42", "https://github.com/octo/demo/pull/42"), m_q)

    chk(
        "is_safe_relpath rejects traversal, double slash, trailing slash, null",
        bb.is_safe_relpath("app/main.py")
        and not bb.is_safe_relpath("a//b.py")
        and not bb.is_safe_relpath("a/")
        and not bb.is_safe_relpath("a/\x00b")
        and not bb.is_safe_relpath("../x")
        and not bb.is_safe_relpath("./x"),
    )

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
        chk("secret .env excluded from adherence.patch",
            "SECRET_KEY" not in open(os.path.join(bundle, "lanes", "adherence.patch"), encoding="utf-8").read())
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

    # 3. Docs-only diff skips code-quality and conventions AND ignores markdown prose signals
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
        write(repo, "README.md", "# Docs\nDescribes auth token, model schema, async worker, and N+1 cache.\n" + "".join(f"Paragraph {i}\n" for i in range(20)))
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "docs: expand readme")

        p = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", bundle, "--effort", "high", "--local-only"],
            capture_output=True, text=True,
        )
        s_docs = json.loads(p.stdout)
        sel = [x["lane"] for x in s_docs["lane_plan"]["selected"]]
        chk("docs-only diff skips code-quality, conventions, and doc-prose conditional lanes",
            sel == ["adherence"] and s_docs["signals"] == {}, (sel, s_docs["signals"]))

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
        write(repo, "src/build/runner.py", "def run_build():\n    return True\n")
        write(repo, "pkg/routes.py", "def v1_router(req):\n    return {'ok': True}\n")
        write(repo, "tests/test_auth.py", "def test_ok():\n    assert True\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "initial codebase")
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
        chk("CODEBASE file-map.json tracks repo files (including src/build/runner.py) and excludes .env",
            "src/auth.py" in fmap and "src/build/runner.py" in fmap and "pkg/routes.py" in fmap
            and ".env" not in fmap and fmap["src/auth.py"]["lines"] == 2,
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

        # CODEBASE + --effort=low emits critical-risk.files.txt and files_list
        b_cb_low = os.path.join(td, "b-cb-low")
        p_cb_low = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--level=CODEBASE", "--effort=low", "--lanes=security", "--cwd", repo, "--bundle-dir", b_cb_low],
            capture_output=True, text=True,
        )
        s_cb_low = json.loads(p_cb_low.stdout)
        chk("CODEBASE --effort=low writes lanes/critical-risk.files.txt and files_list",
            p_cb_low.returncode == 0
            and os.path.isfile(os.path.join(b_cb_low, "lanes", "critical-risk.files.txt"))
            and s_cb_low["lane_plan"]["selected"][0].get("files_list") == "lanes/critical-risk.files.txt"
            and s_cb_low["lane_plan"].get("focus_lanes") == ["security"],
            s_cb_low["lane_plan"])

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
            and set(fmap_sub.keys()) == {"src/auth.py", "src/db.py", "src/worker.py", "src/build/runner.py"}, fmap_sub)

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

        # Reject --effort=skim, PR URLs, invalid --lanes, invalid --shard-max-files, and nonexistent scope_path
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

        p_bad_ln = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--level=CODEBASE", "--lanes=bogus", "--cwd", repo, "--bundle-dir", b_cb],
            capture_output=True, text=True,
        )
        chk("invalid --lanes exits 2", p_bad_ln.returncode == 2, p_bad_ln.stderr)

        p_bad_sh = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--level=CODEBASE", "--shard-max-files=0", "--cwd", repo, "--bundle-dir", b_cb],
            capture_output=True, text=True,
        )
        chk("--shard-max-files=0 exits 2", p_bad_sh.returncode == 2, p_bad_sh.stderr)

        p_bad_sc = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "nonexistent/", "--level=CODEBASE", "--cwd", repo, "--bundle-dir", b_cb],
            capture_output=True, text=True,
        )
        chk("CODEBASE nonexistent scope exits 2", p_bad_sc.returncode == 2, p_bad_sc.stderr)

    # 5. Diff parsing, signals, classifiers, skim manifests, low core_lines, ReDoS
    diff_complex = (
        'diff --git "a/dir/caf\\303\\251.py" "b/dir/caf\\303\\251.py"\n'
        '--- "a/dir/caf\\303\\251.py"\n'
        '+++ "b/dir/caf\\303\\251.py"\n'
        "@@ -1,1 +1,2 @@\n"
        "+++ added_plus_plus\n"
        "--- removed_sql_comment\n"
        " ctx\n"
        "diff --git a/old name.py b/new name.py\n"
        "similarity index 90%\n"
        "rename from old name.py\n"
        "rename to new name.py\n"
        "--- a/old name.py\n"
        "+++ b/new name.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/dir b/x.py b/dir b/x.py\n"
        "--- a/dir b/x.py\n"
        "+++ b/dir b/x.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+pass\n"
        "diff --git a/img.png b/img.png\n"
        "Binary files a/img.png and b/img.png differ\n"
    )
    secs = bb.parse_diff_by_file(diff_complex)
    chk(
        "parse_diff_by_file handles quoted UTF-8, +++/--- in hunks, renames, ' b/', binaries",
        len(secs) == 4
        and secs[0]["path"] == "dir/café.py"
        and secs[0]["adds"] == 1
        and secs[0]["dels"] == 1
        and secs[1]["status"] == "renamed"
        and secs[1]["old_path"] == "old name.py"
        and secs[1]["path"] == "new name.py"
        and secs[2]["path"] == "dir b/x.py"
        and secs[3]["binary"] is True,
        secs,
    )

    sig_secs = bb.parse_diff_by_file(
        "diff --git a/ui/App.tsx b/ui/App.tsx\n--- a/ui/App.tsx\n+++ b/ui/App.tsx\n@@ -0,0 +1,1 @@\n+export const A = () => <div/>;\n"
        "diff --git a/db/001.sql b/db/001.sql\n--- a/db/001.sql\n+++ b/db/001.sql\n@@ -0,0 +1,1 @@\n+CREATE TABLE t(id int);\n"
        "diff --git a/v1/users.proto b/v1/users.proto\n--- a/v1/users.proto\n+++ b/v1/users.proto\n@@ -0,0 +1,1 @@\n+message U {}\n"
        "diff --git a/app/orders.py b/app/orders.py\n--- a/app/orders.py\n+++ b/app/orders.py\n@@ -1,2 +1,1 @@\n-@login_required\n def order(): pass\n"
        "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n@@ -0,0 +1,1 @@\n+requests==2.32.0\n"
    )
    sigs = bb.detect_signals(sig_secs)
    chk(
        "detect_signals detects .tsx (ui), .sql (data), v1/+.proto (contract), removed - @login_required (auth), requirements.txt (deps)",
        "ui/App.tsx" in sigs.get("ui", [])
        and "db/001.sql" in sigs.get("data", [])
        and "v1/users.proto" in sigs.get("contract", [])
        and "app/orders.py" in sigs.get("auth", [])
        and "requirements.txt" in sigs.get("deps", []),
        sigs,
    )

    t0 = time.monotonic()
    bb.SIGNAL_RULES["perf"].search("for x in items: " + ("a " * 20000))
    elapsed = time.monotonic() - t0
    chk("perf signal regex has no ReDoS on long non-matching line", elapsed < 0.2, elapsed)

    chk(
        "is_spec_file filters tasks/*.py and allows tasks/*.md; is_rule_file matches nested copilot instructions",
        not bb.is_spec_file("tasks/worker.py")
        and not bb.is_spec_file("plans/billing.py")
        and not bb.is_spec_file("specs/auth_spec.rb")
        and bb.is_spec_file("tasks/roadmap.md")
        and bb.is_rule_file("packages/web/.github/copilot-instructions.md")
        and bb.is_denylisted("Config/.ENV.local")
        and bb.is_secret_or_vendor("keys/ID_RSA"),
    )

    # 6. Skim keeps manifests in diff.skim.patch, test-only skips 0-file lanes, low uses core_lines
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "skim-repo")
        os.makedirs(repo)
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")
        write(repo, "package.json", '{"dependencies": {"lodash": "4.17.20"}}\n')
        write(repo, "package-lock.json", '{"lockfileVersion": 3}\n')
        write(repo, "tasks/worker.py", "def run():\n    return 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "base")

        git(repo, "checkout", "-b", "feat/deps")
        write(repo, "package.json", '{"dependencies": {"lodash": "4.17.21"}}\n')
        write(repo, "package-lock.json", '{"lockfileVersion": 3, "v": 2}\n')
        write(repo, "tasks/worker.py", "def run():\n    return 2\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "chore: bump lodash and worker")

        b_sk = os.path.join(td, "b-sk")
        p_sk = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_sk, "--effort", "auto", "--local-only"],
            capture_output=True, text=True,
        )
        s_sk = json.loads(p_sk.stdout)
        chk("tasks/worker.py does not count as spec_file in auto effort",
            s_sk["intent_hints"]["diff_specs"] == [] and s_sk["effort"]["resolved"] == "skim", s_sk["effort"])

        b_sk2 = os.path.join(td, "b-sk2")
        subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_sk2, "--effort", "skim", "--local-only"],
            capture_output=True, text=True, check=True,
        )
        d_skim = open(os.path.join(b_sk2, "diff.skim.patch"), encoding="utf-8").read()
        d_lock = open(os.path.join(b_sk2, "lock.patch"), encoding="utf-8").read()
        chk("skim keeps package.json in diff.skim.patch and puts only lockfiles in lock.patch",
            "package.json" in d_skim and "package-lock.json" not in d_skim and "package-lock.json" in d_lock and "package.json" not in d_lock,
            (d_skim, d_lock))

        # Test-only diff matching signals must skip 0-file conditional lanes (no empty micro-fold)
        git(repo, "checkout", "-b", "feat/test-only", "main")
        write(repo, "tests/test_perf.py", "def test_cache_async():\n    # redis async pagination\n    assert True\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "test: add cache async test")
        b_test = os.path.join(td, "b-test")
        p_test = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_test, "--effort", "high", "--local-only"],
            capture_output=True, text=True,
        )
        s_test = json.loads(p_test.stdout)
        chk("test-only PR skips 0-file performance/robustness lanes instead of folding 0 lines",
            not s_test["lane_plan"]["folded"]
            and all(x["lane"] not in ("performance", "robustness") for x in s_test["lane_plan"]["selected"]),
            s_test["lane_plan"])

        # Low effort inline threshold uses core_lines (500 test lines + 10 core lines -> inline!)
        write(repo, "tests/test_big.py", "".join(f"def test_case_{i}():\n    assert {i} == {i}\n" for i in range(250)))
        write(repo, "tasks/worker.py", "def run():\n    return 3\n")
        write(repo, "package.json", '{"dependencies": {"lodash": "4.17.22"}}\n')
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "test: large test file")
        b_low_inline = os.path.join(td, "b-low-inline")
        p_li = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_low_inline, "--effort", "low", "--lanes=security", "--local-only"],
            capture_output=True, text=True,
        )
        s_li = json.loads(p_li.stdout)
        chk("low effort uses core_lines (<400 despite >400 test lines), keeps manifests in diff.core.patch, and preserves --lanes focus",
            s_li["lane_plan"]["selected"][0]["tier"] == "inline"
            and s_li["lane_plan"].get("focus_lanes") == ["security"]
            and "package.json" in open(os.path.join(b_low_inline, "diff.core.patch"), encoding="utf-8").read(),
            s_li["lane_plan"])

    # 7. Helpers: CI, bot detection, paginated JSON, sanitize_pr_data, persona, intent_hints, find_clone, find_context_files
    chk(
        "is_bot_author detects bots without matching human names like talbot/cabot",
        bb.is_bot_author({"login": "dependabot[bot]"})
        and bb.is_bot_author({"login": "renovate", "is_bot": False})
        and bb.is_bot_author({"login": "ci-user", "is_bot": True})
        and not bb.is_bot_author({"login": "talbot", "is_bot": False})
        and not bb.is_bot_author({"login": "cabot", "is_bot": False}),
    )
    chk(
        "compute_ci detects STARTUP_FAILURE, SKIPPED, and IN_PROGRESS",
        bb.compute_ci([{"name": "build", "conclusion": "STARTUP_FAILURE", "status": "COMPLETED"}]).startswith("failing")
        and bb.compute_ci([{"name": "lint", "conclusion": "SKIPPED", "status": "COMPLETED"}]) == "passing"
        and bb.compute_ci([{"name": "test", "conclusion": None, "status": "IN_PROGRESS"}]) == "pending",
    )

    pg_items, pg_ok = bb.parse_paginated_json_list('[{"id": 1}]\n[\n  {"id": 2}\n]')
    nd_items, nd_ok = bb.parse_paginated_json_list('{"id": 3}\n{"id": 4}')
    chk(
        "parse_paginated_json_list parses multi-array and NDJSON streams",
        pg_ok and [x["id"] for x in pg_items] == [1, 2] and nd_ok and [x["id"] for x in nd_items] == [3, 4],
    )

    san = bb.sanitize_pr_data({
        "author": {"login": "talbot", "name": "Human Name", "is_bot": False},
        "commits": [{"messageHeadline": "fix bug", "authors": [{"email": "secret@example.com", "name": "Secret"}]}],
    })
    chk(
        "sanitize_pr_data strips commit author emails and author.name",
        san["author"] == {"login": "talbot", "is_bot": False}
        and san["commits"] == [{"messageHeadline": "fix bug"}],
        san,
    )

    hints = bb.extract_intent_hints(
        {
            "title": "Fix #42 (see https://docs.example.com/spec.)",
            "body": "Refs octo/other#99 and PROJ-10 and PD-1",
            "headRefName": "feat/x",
            "commits": [{"messageHeadline": "Also fixes #43"}],
        },
        [],
        None,
        "",
        [],
        owner="octo",
        repo="demo",
    )
    chk(
        "extract_intent_hints parses #N, owner/repo#N, strips URL punctuation, and drops PD-*",
        "https://github.com/octo/demo/issues/42" in hints["github_issues"]
        and "https://github.com/octo/demo/issues/43" in hints["github_issues"]
        and "https://github.com/octo/other/issues/99" in hints["github_issues"]
        and "https://docs.example.com/spec" in hints["external_urls"]
        and hints["tickets"] == ["PROJ-10"],
        hints,
    )

    with tempfile.TemporaryDirectory() as td:
        pf = os.path.join(td, "persona.md")
        write(
            td,
            "persona.md",
            "---\n"
            "status: approved\n"
            "refreshed: 2026-05-01\n"
            "memory: enabled\n"
            "voice:\n"
            "  tone: concise\n"
            "  spelling: american\n"
            "  max_inline: 6\n"
            "  hold_event: COMMENT\n"
            "  comment_prefix: \"[{SEVERITY} / {CONFIDENCE}]\" # prefix note\n"
            "  nit_prefix: \"Nit:\"\n"
            "  emoji: sparse\n"
            "emphasis:\n"
            "  - security\n"
            "---\n# Body\n## 8. House rules\n1. Check XYZ.\n## 9. Learned preferences\n1. [check] (all) Verify ABC.\n",
        )
        pinfo = bb.parse_persona_frontmatter(pf)
        psum = bb._persona_summary(pinfo)
        chk(
            "parse_persona_frontmatter extracts full voice keys and block emphasis, and _persona_summary preserves emphasis",
            pinfo["voice"]["comment_prefix"] == "[{SEVERITY} / {CONFIDENCE}]"
            and pinfo["voice"]["nit_prefix"] == "Nit:"
            and pinfo["voice"]["emoji"] == "sparse"
            and pinfo["voice"]["spelling"] == "american"
            and pinfo["emphasis"] == ["security"]
            and psum.get("emphasis") == ["security"]
            and pinfo["has_house_rules"]
            and pinfo["has_learned_prefs"],
            (pinfo, psum),
        )

        # find_clone exact owner/repo + find_context_files dash-filename safety
        c_repo = os.path.join(td, "clone-check")
        os.makedirs(c_repo)
        git(c_repo, "init", "-b", "main")
        git(c_repo, "config", "user.email", "t@example.com")
        git(c_repo, "config", "user.name", "T")
        git(c_repo, "remote", "add", "origin", "https://github.com/Octo/Demo-v2.git")
        write(c_repo, "-dash.py", "def dash_fn():\n    return 1\n")
        write(c_repo, "caller.py", "import something\n# -dash usage\n")
        git(c_repo, "add", "-A")
        git(c_repo, "commit", "-m", "init")
        sha = git(c_repo, "rev-parse", "HEAD")
        chk("find_clone rejects substring repo match Octo/Demo-v2 for octo/demo",
            bb.find_clone(c_repo, "octo", "demo") is None
            and bb.find_clone(c_repo, "octo", "demo-v2") is not None)
        ctx = bb.find_context_files(c_repo, sha, ["-dash.py"], 5, 500)
        chk("find_context_files handles dash-prefixed filename safely via -e",
            [p for p, _ in ctx] == ["caller.py"], ctx)

        b_pers = os.path.join(td, "b-pers")
        p_pers = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--level=CODEBASE", "--cwd", c_repo, "--bundle-dir", b_pers, "--persona", pf],
            capture_output=True, text=True,
        )
        s_pers = json.loads(p_pers.stdout)
        chk("bundle-summary.json preserves persona.emphasis for finalize Pareto tie-breaking",
            p_pers.returncode == 0 and s_pers["persona"].get("emphasis") == ["security"],
            s_pers.get("persona"))

    # 8. Carry delta_mode, last_sha==head_sha guard, corrupt findings.json skip, legacy dir, and non-git symlink block
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "carry-repo")
        rev_dir = os.path.join(td, "reviews")
        leg_dir = os.path.join(td, "legacy-reviews")
        os.makedirs(repo)
        os.makedirs(rev_dir)
        os.makedirs(leg_dir)
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "T")
        write(repo, "a.py", "def a():\n    return 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "c0")
        git(repo, "checkout", "-b", "feat/carry")
        write(repo, "a.py", "def a():\n    return 2\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "c1")
        sha1 = git(repo, "rev-parse", "HEAD")

        # Save previous findings at sha1
        prev_folder = os.path.join(rev_dir, f"local-carry-repo-0-{sha1[:7]}")
        os.makedirs(prev_folder)
        write(
            prev_folder,
            "findings.json",
            json.dumps({
                "pr": "local/carry-repo#0",
                "last_sha": sha1,
                "next_id": {"D": 2},
                "findings": [{
                    "id": "D1", "lane": "code-quality", "severity": "HIGH", "confidence": "HIGH",
                    "title": "Old issue", "location": "a.py:2", "issue": "Old bug", "fix": "Fix",
                    "trigger": "Call a()", "status": "todo",
                }],
            }),
        )

        # Same SHA + --since-last must NOT create empty diff
        b_same = os.path.join(td, "b-same")
        p_same = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_same, "--reviews-dir", rev_dir, "--since-last", "--local-only"],
            capture_output=True, text=True,
        )
        s_same = json.loads(p_same.stdout)
        chk("--since-last when last_sha == head_sha keeps full diff (not empty delta)",
            not s_same["carry"]["delta_mode"] and s_same["pr"]["changed_files"] == 1, s_same["carry"])

        # Advance commit -> delta_mode triggers and writes pr.diff.patch
        write(repo, "b.py", "def b():\n    return 3\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "c2")
        b_delta = os.path.join(td, "b-delta")
        p_delta = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_delta, "--reviews-dir", rev_dir, "--local-only"],
            capture_output=True, text=True,
        )
        s_delta = json.loads(p_delta.stdout)
        chk("delta_mode creates pr.diff.patch and slices only c2 in diff.patch",
            s_delta["carry"]["delta_mode"]
            and os.path.isfile(os.path.join(b_delta, "pr.diff.patch"))
            and "b.py" in open(os.path.join(b_delta, "diff.patch"), encoding="utf-8").read()
            and "a.py" not in open(os.path.join(b_delta, "diff.patch"), encoding="utf-8").read(),
            s_delta["carry"])

        # Corrupt and schema-invalid findings.json both skip carry gracefully without crashing
        write(prev_folder, "findings.json", "{corrupt json")
        b_corrupt = os.path.join(td, "b-corrupt")
        p_corrupt = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_corrupt, "--reviews-dir", rev_dir, "--local-only"],
            capture_output=True, text=True,
        )
        s_corrupt = json.loads(p_corrupt.stdout)
        chk("corrupt previous findings.json skips carry with warning and exits 0",
            p_corrupt.returncode == 0 and not s_corrupt["carry"]["enabled"]
            and any("carry skipped" in w for w in s_corrupt["coverage_limits"]),
            s_corrupt)

        write(prev_folder, "findings.json", json.dumps({"next_id": {"D": 2}, "findings": [{"severity": "BOGUS"}]}))
        b_bad_carry = os.path.join(td, "b-bad-carry")
        p_bad_carry = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--cwd", repo, "--bundle-dir", b_bad_carry, "--reviews-dir", rev_dir, "--local-only"],
            capture_output=True, text=True,
        )
        s_bad_carry = json.loads(p_bad_carry.stdout)
        chk("schema-invalid previous findings.json catches BadInput, skips carry, and exits 0",
            p_bad_carry.returncode == 0 and not s_bad_carry["carry"]["enabled"]
            and any("carry skipped" in w for w in s_bad_carry["coverage_limits"]),
            s_bad_carry)

        # Legacy review dir fallback
        leg_folder = os.path.join(leg_dir, "octo-demo-7-1234567")
        os.makedirs(leg_folder)
        write(leg_folder, "findings.json", json.dumps({"next_id": {"B": 2}, "findings": []}))
        chk("find_previous_findings searches legacy extra_dirs",
            bb.find_previous_findings(os.path.join(td, "missing-dir"), "octo", "demo", 7, extra_dirs=[leg_dir])
            == os.path.join(leg_folder, "findings.json"))

        # Non-git build_codebase symlink block + scoped carry filter
        outside = os.path.join(td, "outside_secret.txt")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("TOP_SECRET_VALUE\n")
        nongit = os.path.join(td, "nongit")
        os.makedirs(os.path.join(nongit, "src", "build"))
        write(nongit, "src/build/app.py", "def ok():\n    return 1\n")
        os.symlink(outside, os.path.join(nongit, "src", "leak.py"))
        b_ng = os.path.join(td, "b-ng")
        p_ng = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "--level=CODEBASE", "--cwd", nongit, "--bundle-dir", b_ng],
            capture_output=True, text=True,
        )
        fmap_ng = json.load(open(os.path.join(b_ng, "file-map.json")))
        chk("non-git CODEBASE skips symlinks outside cwd and keeps src/build/app.py",
            p_ng.returncode == 0 and "src/build/app.py" in fmap_ng and "src/leak.py" not in fmap_ng,
            fmap_ng)

        # Scoped CODEBASE carry filters findings outside scope_path while preserving next_id
        cb_prev = os.path.join(rev_dir, "local-nongit-codebase-worktree")
        os.makedirs(cb_prev)
        write(
            cb_prev,
            "findings.json",
            json.dumps({
                "pr": "local/nongit#codebase",
                "scope_path": ".",
                "next_id": {"S": 3},
                "findings": [
                    {
                        "id": "S1", "lane": "security", "severity": "HIGH", "confidence": "HIGH",
                        "title": "In scope", "location": "src/build/app.py:1", "issue": "In scope",
                        "fix": "Fix", "trigger": "Call ok()", "status": "todo",
                    },
                    {
                        "id": "S2", "lane": "security", "severity": "HIGH", "confidence": "HIGH",
                        "title": "Out of scope", "location": "pkg/other.py:1", "issue": "Out of scope",
                        "fix": "Fix", "trigger": "Call other()", "status": "todo",
                    },
                ],
            }),
        )
        b_sc_carry = os.path.join(td, "b-sc-carry")
        p_sc_carry = subprocess.run(
            [sys.executable, "-I", "-B", SCRIPT, "src/", "--level=CODEBASE", "--cwd", nongit, "--bundle-dir", b_sc_carry, "--reviews-dir", rev_dir],
            capture_output=True, text=True,
        )
        s_sc_carry = json.loads(p_sc_carry.stdout)
        open_sc = json.load(open(os.path.join(b_sc_carry, "open.json")))
        prev_sc = json.load(open(os.path.join(b_sc_carry, "previous.json")))
        chk("scoped CODEBASE carry filters out-of-scope findings while keeping next_id",
            p_sc_carry.returncode == 0
            and [x["id"] for x in open_sc] == ["S1"]
            and prev_sc["next_id"]["S"] == 3
            and s_sc_carry["carry"]["scope_path"] == "src",
            (open_sc, prev_sc, s_sc_carry["carry"]))

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
