#!/usr/bin/env python3
"""Run a baseline schema_lint.py and the current one on the fixtures. Usage:
python3 -I -B compare_schema_lint.py <baseline schema_lint.py>
Legacy fixture: stdout, stderr and exit code must be identical. New-field fixture: print both."""
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
cur = os.path.normpath(os.path.join(here, "..", "..", "schema_lint.py"))
base = sys.argv[1]


def run(script, fx):
    r = subprocess.run([sys.executable, "-I", "-B", script, os.path.join(here, fx),
                        os.path.join(here, "diff-map.json")], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


a, b = run(base, "legacy-findings.json"), run(cur, "legacy-findings.json")
print("legacy fixture identical:", a == b, "(exit", a[0], ", stdout bytes", len(a[1]), ")")
n_base, n_cur = run(base, "new-fields-findings.json"), run(cur, "new-fields-findings.json")
print("new-field fixture differs (expected):", n_base != n_cur)
import json
for r in json.loads(n_cur[1])["results"]:
    print(" ", r["id"], r["action"], r["reasons"])
sys.exit(0 if a == b else 1)
