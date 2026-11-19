#!/usr/bin/env python3
"""Regression tests for scripts/ste_lint.py (Gate G8 + question refreshers). Run: python3 -I -B test_ste_lint.py"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "ste_lint.py"))
spec = importlib.util.spec_from_file_location("ste_lint", SCRIPT)
ste_lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ste_lint)


def rules_of(text):
    return [rule for _, rule, _ in ste_lint.lint(text)]


def run_tests():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    good_q = (
        "### Questions for the author\n"
        "1. **Q1** (`app/worker.py:88`) — Empty batch payloads currently return HTTP 200. "
        "Should this endpoint reject empty batches with HTTP 400?\n"
    )
    chk("compliant question with 1-sentence refresher passes", not ste_lint.lint(good_q), ste_lint.lint(good_q))

    missing_ref = (
        "### Questions for the author\n"
        "1. **Q1** (`app/worker.py:88`) — Should this endpoint reject empty batches?\n"
    )
    chk("missing refresher in Questions section flagged", "question-missing-refresher" in rules_of(missing_ref), rules_of(missing_ref))

    over_one = (
        "### Questions for the author\n"
        "1. **Q1** (`app/worker.py:88`) — Empty batch payloads arrive from cron. "
        "They currently return HTTP 200. Should this endpoint reject empty batches?\n"
    )
    chk(">1 sentence refresher in Questions section flagged", "question-refresher-over-1-sentence" in rules_of(over_one), rules_of(over_one))

    bare_ids_1 = "Should we drop X1 and Y2?"
    chk("bare-id-question catches X1/Y2 without refresher", "bare-id-question" in rules_of(bare_ids_1), rules_of(bare_ids_1))

    bare_ids_2 = "Does AC3 apply to internal health checks?"
    chk("bare-id-question catches AC3 without refresher", "bare-id-question" in rules_of(bare_ids_2), rules_of(bare_ids_2))

    bare_ids_3 = "1. **Q1** (`app/auth.py:12`) — Should we drop X1?"
    chk("bare-id-question catches Q1 label with bare X1 in question", "bare-id-question" in rules_of(bare_ids_3), rules_of(bare_ids_3))

    good_id_q = "AC3 requires audit logs for failed logins. Does AC3 apply to internal health checks?"
    chk("ID question with 1-sentence refresher passes", not ste_lint.lint(good_id_q), ste_lint.lint(good_id_q))

    inline_id_q = "X1 — The parser has no null check: should we drop X1?"
    chk("ID question with inline colon refresher passes", not ste_lint.lint(inline_id_q), ste_lint.lint(inline_id_q))

    multi_q = (
        "### Questions for the author\n"
        "1. **Q1** (`app/worker.py:88`) — Empty batch payloads return HTTP 200. Should we reject empty batches?\n"
        "2. **Q2** (`app/auth.py:12`) — Should we skip X1?\n"
    )
    r_multi = ste_lint.lint(multi_q)
    chk("multi-item list flags only the item lacking refresher",
        len(r_multi) == 2 and all(line == 3 for line, _, _ in r_multi)
        and {rule for _, rule, _ in r_multi} == {"question-missing-refresher", "bare-id-question"},
        r_multi)

    cb_good_q = (
        "### Open questions\n"
        "1. **Q1** (`app/worker.py:88`) — Retry jobs lack a dead-letter queue. "
        "Should failed jobs move to a dead-letter topic after three attempts?\n"
    )
    chk("CODEBASE Open questions with 1-sentence refresher passes", not ste_lint.lint(cb_good_q), ste_lint.lint(cb_good_q))

    cb_bad_q = (
        "### Open questions\n"
        "1. **Q1** (`app/worker.py:88`) — Should failed jobs move to a dead-letter topic?\n"
    )
    chk("CODEBASE Open questions without refresher flagged", "question-missing-refresher" in rules_of(cb_bad_q), rules_of(cb_bad_q))

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
