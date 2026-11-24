#!/usr/bin/env python3
"""Unit and CLI tests for scripts/diff_map.py. Run: python3 -I -B test_diff_map.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "diff_map.py"))
spec = importlib.util.spec_from_file_location("diff_map", SCRIPT)
dm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dm)


def run_tests():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    # 1. Basic added, context, hunks, multiple hunks, omitted ,count
    diff_basic = (
        "diff --git a/app/a.py b/app/a.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/a.py\n"
        "+++ b/app/a.py\n"
        "@@ -1,3 +1,4 @@ def foo():\n"
        " line1\n"
        "+added2\n"
        "-old2\n"
        "+added3\n"
        " line4\n"
        "@@ -20 +21 @@\n"
        "-old21\n"
        "+new21\n"
    )
    res1 = dm.build(diff_basic)
    chk(
        "basic added/context/hunks",
        res1 == {
            "app/a.py": {
                "added": [2, 3, 21],
                "context": [1, 4],
                "hunks": [
                    {"old_start": 1, "old_count": 3, "new_start": 1, "new_count": 4},
                    {"old_start": 20, "old_count": 1, "new_start": 21, "new_count": 1},
                ],
            }
        },
        res1,
    )

    # 2. Adversarial hunk body lines (+++, ---, diff --git, @@ inside hunk) + \ No newline at end of file
    diff_adv = (
        "diff --git a/sql/q.sql b/sql/q.sql\n"
        "--- a/sql/q.sql\n"
        "+++ b/sql/q.sql\n"
        "@@ -1,2 +1,4 @@\n"
        "+++ x\n"
        "--- old sql comment\n"
        "+diff --git a/fake b/fake\n"
        "+@@ -99 +99 @@\n"
        " context_end\n"
        "\\ No newline at end of file\n"
        "diff --git a/app/b.py b/app/b.py\n"
        "--- a/app/b.py\n"
        "+++ b/app/b.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x\n"
        "+y\n"
    )
    res_adv = dm.build(diff_adv)
    chk(
        "hunk body +++/---/@@ lines do not hijack path or leak into next file",
        set(res_adv.keys()) == {"sql/q.sql", "app/b.py"}
        and res_adv["sql/q.sql"]["added"] == [1, 2, 3]
        and res_adv["sql/q.sql"]["context"] == [4]
        and res_adv["app/b.py"]["added"] == [1],
        res_adv,
    )

    # 3. Deleted file (+++ /dev/null) and pure-deletion hunk (+0,0 / +9,0) and binary file
    diff_del_bin = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-a\n"
        "-b\n"
        "diff --git a/img/logo.png b/img/logo.png\n"
        "Binary files a/img/logo.png and b/img/logo.png differ\n"
        "diff --git a/keep.py b/keep.py\n"
        "--- a/keep.py\n"
        "+++ b/keep.py\n"
        "@@ -5,2 +5,0 @@\n"
        "-dead1\n"
        "-dead2\n"
    )
    res_del = dm.build(diff_del_bin)
    chk(
        "deleted/binary files excluded and pure-deletion hunk keeps empty added list",
        "gone.py" not in res_del
        and "img/logo.png" not in res_del
        and res_del.get("keep.py", {}).get("added") == []
        and len(res_del.get("keep.py", {}).get("hunks", [])) == 1,
        res_del,
    )

    # 4. Quoted git paths, UTF-8 escapes, spaces, and paths containing ' b/'
    diff_paths = (
        'diff --git "a/dir/caf\\303\\251.py" "b/dir/caf\\303\\251.py"\n'
        '--- "a/dir/caf\\303\\251.py"\n'
        '+++ "b/dir/caf\\303\\251.py"\n'
        "@@ -0,0 +1,1 @@\n"
        "+x = 1\n"
        "diff --git a/dir b/file name.py b/dir b/file name.py\n"
        "--- a/dir b/file name.py\n"
        "+++ b/dir b/file name.py\n"
        "@@ -1,1 +1,2 @@\n"
        " a = 1\n"
        "+b = 2\n"
        'diff --git "a/a \\"q\\".txt" "b/a \\"q\\".txt"\n'
        '--- "a/a \\"q\\".txt"\n'
        '+++ "b/a \\"q\\".txt"\n'
        "@@ -0,0 +1,1 @@\n"
        "+ok\n"
    )
    res_paths = dm.build(diff_paths)
    chk(
        "quoted UTF-8, spaces, and ' b/' path headers parsed cleanly",
        res_paths.get("dir/café.py", {}).get("added") == [1]
        and res_paths.get("dir b/file name.py", {}).get("added") == [2]
        and res_paths.get('a "q".txt', {}).get("added") == [1],
        res_paths,
    )

    # 5. Stale new_line isolation when a file has empty header/rename without hunks + bare unified diff
    diff_rename_bare = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -10,1 +10,2 @@\n"
        " keep\n"
        "+add11\n"
        "--- a/bare1.py\t2026-01-01 00:00:00\n"
        "+++ b/bare1.py\t2026-01-01 00:00:00\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    res_rb = dm.build(diff_rename_bare)
    chk(
        "pure rename and bare unified diff with timestamp parsed",
        res_rb.get("a.py", {}).get("added") == [11]
        and res_rb.get("bare1.py", {}).get("added") == [1],
        res_rb,
    )

    # 6. parse_diff_git_paths helper edge cases
    chk(
        "parse_diff_git_paths handles mixed quoted/unquoted and ' b/'",
        dm.parse_diff_git_paths('diff --git a/plain.py "b/caf\\303\\251.py"') == ("plain.py", "café.py")
        and dm.parse_diff_git_paths("diff --git a/dir b/x.py b/dir b/x.py") == ("dir b/x.py", "dir b/x.py"),
    )

    # 7. CLI exit codes (0 valid, 2 missing file, 2 missing arg)
    with tempfile.TemporaryDirectory() as td:
        patch = os.path.join(td, "sample.patch")
        with open(patch, "w", encoding="utf-8") as fh:
            fh.write(diff_basic)
        p_ok = subprocess.run([sys.executable, "-I", "-B", SCRIPT, patch], capture_output=True, text=True)
        p_miss = subprocess.run([sys.executable, "-I", "-B", SCRIPT, os.path.join(td, "none.patch")], capture_output=True, text=True)
        p_arg = subprocess.run([sys.executable, "-I", "-B", SCRIPT], capture_output=True, text=True)
        chk(
            "CLI exits 0 on valid diff and 2 on invalid args/missing file",
            p_ok.returncode == 0
            and json.loads(p_ok.stdout)["app/a.py"]["added"] == [2, 3, 21]
            and p_miss.returncode == 2
            and p_arg.returncode == 2,
            (p_ok.returncode, p_miss.returncode, p_arg.returncode),
        )

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
