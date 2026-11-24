#!/usr/bin/env python3
"""Regression tests for scripts/ste_lint.py (Gate G8 + question refreshers). Run: python3 -I -B test_ste_lint.py"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "ste_lint.py"))
spec = importlib.util.spec_from_file_location("ste_lint", SCRIPT)
ste_lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ste_lint)


def rules_of(text, findings=None):
    return [rule for _, rule, _ in ste_lint.lint(text, findings=findings)]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = ste_lint.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


def run_tests():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    # --- 1. Question refreshers & bare-id-question ---
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
    chk(
        "multi-item list flags only the item lacking refresher",
        len(r_multi) == 2 and all(line == 3 for line, _, _ in r_multi)
        and {rule for _, rule, _ in r_multi} == {"question-missing-refresher", "bare-id-question"},
        r_multi,
    )

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

    # --- 2. New bare-id-question & Questions parser edge cases ---
    chk("backticked bare IDs caught", "bare-id-question" in rules_of("Should we drop `X1` and `H2`?"), rules_of("Should we drop `X1` and `H2`?"))
    chk("quoted bare ID caught", "bare-id-question" in rules_of('Should we drop "X1"?'), rules_of('Should we drop "X1"?'))
    chk("Y2 alone caught", "bare-id-question" in rules_of("Should we drop Y2?"), rules_of("Should we drop Y2?"))
    chk("code call with v2 in backticks not flagged", not ste_lint.lint("Should `get_v2()` return 404?"), ste_lint.lint("Should `get_v2()` return 404?"))
    chk("S3 storage context not flagged", not ste_lint.lint("Should we store backups in S3?"), ste_lint.lint("Should we store backups in S3?"))
    chk("real S3 review ID flagged", "bare-id-question" in rules_of("Should we drop S3?"), rules_of("Should we drop S3?"))
    chk("API V2 version context not flagged", not ste_lint.lint("Should this route use API V2?"), ste_lint.lint("Should this route use API V2?"))
    chk("real V2 review ID flagged", "bare-id-question" in rules_of("Should we drop V2?"), rules_of("Should we drop V2?"))

    unrelated_prev = "This PR updates login. The tests pass. Should we drop X1?"
    chk("unrelated earlier sentences do not exempt bare X1", "bare-id-question" in rules_of(unrelated_prev), rules_of(unrelated_prev))

    partial_prev = "X1 skips a null check. Should we post X1 and drop Y2?"
    chk("unexplained second ID Y2 still flagged", "bare-id-question" in rules_of(partial_prev), rules_of(partial_prev))

    both_explained = (
        "X1 reports a null dereference in `auth/session.py:44`, and Y2 flags a duplicate lookup in `orders/repo.py:19`. "
        "Should we post X1 and drop Y2?"
    )
    chk("both IDs explained in previous sentence passes", not ste_lint.lint(both_explained), ste_lint.lint(both_explained))

    fake_colon = "Please tell me what to do: should we drop X1?"
    chk("generic colon prefix not naming X1 still flagged", "bare-id-question" in rules_of(fake_colon), rules_of(fake_colon))

    q_colon = "### Questions for the author\n1. **Q1:** (`app/a.py:1`) -- The parser has no null guard: should it return 400?\n"
    chk("Questions section allows inline colon refresher and **Q1:**/--", not ste_lint.lint(q_colon), ste_lint.lint(q_colon))

    q_nonlist_bad = "### Questions for the author\n> **Q1** (`app/a.py:1`) — Should we reject empty batches?\n"
    chk("non-list blockquote question missing refresher flagged", "question-missing-refresher" in rules_of(q_nonlist_bad), rules_of(q_nonlist_bad))

    q_subbullets = (
        "### Questions for the author\n"
        "1. **Q1** (`app/a.py:1`) — Empty batches return HTTP 200. Should we reject them?\n"
        "   - Option A\n"
        "   - Option B\n"
    )
    chk("sub-bullets under question item do not spawn false refresher error", not ste_lint.lint(q_subbullets), ste_lint.lint(q_subbullets))

    # --- 3. Sentence length, prefixes, paragraph length, stacked headers & footers ---
    fix_20_ok = "**Fix:** Add the guard before you read the payload from the database connection pool inside the worker task loop right now."
    chk("20-word instruction after **Fix:** passes", not ste_lint.lint(fix_20_ok), ste_lint.lint(fix_20_ok))

    fix_21_bad = "**Fix:** Reject every invalid payload before you read the record from the database connection pool inside the worker task loop right now."
    chk("21-word instruction after **Fix:** flagged", "sentence-over-20-words" in rules_of(fix_21_bad), rules_of(fix_21_bad))

    nit_21_bad = "Nit: Reject every invalid payload before you read the record from the database connection pool inside the worker task loop right now."
    chk("21-word instruction after Nit: flagged", "sentence-over-20-words" in rules_of(nit_21_bad), rules_of(nit_21_bad))

    sev_21_bad = "[HIGH] Reject every invalid payload before you read the record from the database connection pool inside the worker task loop right now."
    chk("21-word instruction after [HIGH] flagged", "sentence-over-20-words" in rules_of(sev_21_bad), rules_of(sev_21_bad))

    desc_25_ok = "The worker reads one record from the queue and saves it to the local table before the task sends a single reply to the client."
    chk("25-word descriptive sentence passes", not ste_lint.lint(desc_25_ok), ste_lint.lint(desc_25_ok))

    desc_26_bad = "The worker reads one record from the queue and saves it to the primary database table before the background task sends a single reply to the client."
    chk("26-word descriptive sentence flagged", "sentence-over-25-words" in rules_of(desc_26_bad), rules_of(desc_26_bad))

    seven_sents = "One. Two. Three. Four. Five. Six. Seven."
    chk("7-sentence paragraph flagged", "paragraph-over-6-sentences" in rules_of(seven_sents), rules_of(seven_sents))

    bullets_no_period = "\n".join(f"- Item {k} stays short and valid" for k in range(1, 9))
    chk("bullet list items without periods do not merge into >25 words", not ste_lint.lint(bullets_no_period), ste_lint.lint(bullets_no_period))

    pr_report_clean = (
        "## 🔴 Not ready to merge — acme/app#42 · Add caching\n"
        "This PR drops an admin role check and breaks one public endpoint.\n"
        "**Top items:** C1 Admin role check absent — app/admin.py:12. **Counts:** 1 critical · 0 high · 0 medium · 0 low · 0 questions · 0 chat-only\n\n"
        "**Context:** head `abc1234` · draft · spec `docs/spec.md` @ `abc1234` (changed in this PR) · tickets PROJ-12 ✓ · PR description ok.\n"
        "**Gaps:** PROJ-13 ticket body is not readable.   **Already raised:** 2 (Gate G5)\n"
        "**High risk:** auth middleware changed.\n\n"
        "### Must fix (CRITICAL, HIGH)\n"
        "**C1 · Admin role check absent** — `app/admin.py:12`\n"
        "The delete route reads the user token and deletes the row without a check for the admin role.\n"
        "**Fix:** Verify that the caller holds the admin role before you delete the record from the database table.\n\n"
        "---\n"
        "Effort: high · Lanes: security ✓ · Quality gate: FAIL · Findings: 2 raw → 1 merged → 1 post (0 chat-only) · Gates removed: 0 out-of-diff · Persona: neutral (none) · Language: ASD-STE100, G8 clean\n"
    )
    chk("full PR report template with stacked headers passes", not ste_lint.lint(pr_report_clean), ste_lint.lint(pr_report_clean))

    cb_report_clean = (
        "## 🟡 Codebase needs targeted improvements — acme/app @ `abc1234` (Codebase review)\n"
        "The codebase has one unbounded query and one stale spec note.\n"
        "**Top items:** M1 Unbounded query — app/repo.py:55. **Counts:** 0 critical · 0 high · 1 medium · 0 low · 0 questions · 0 chat-only\n\n"
        "**Scope:** level `CODEBASE` · head `abc1234` · path `.` · 42 files (3200 lines) · specs docs/arch.md · rules CLAUDE.md.\n"
        "**Gaps:** none.\n\n"
        "### Recommended improvements (MEDIUM)\n"
        "**M1 · Unbounded query** — `app/repo.py:55`\n"
        "The query loads every row into memory with no page limit.\n"
        "**Fix:** Add a page size parameter to the query.\n\n"
        "---\n"
        "Effort: high · Level: CODEBASE · Lanes: performance ✓ · Quality gate: CONCERNS · Findings: 1 raw → 1 merged → 1 kept (0 chat-only) · Gates removed: 0 invalid-location · Persona: neutral (none) · Language: ASD-STE100, G8 clean\n"
    )
    chk("full CODEBASE report template passes", not ste_lint.lint(cb_report_clean), ste_lint.lint(cb_report_clean))

    cb_legacy_footer = "Level: CODEBASE · Effort: high · Lanes: security ✓ · Quality gate: FAIL · Findings: 4 raw → 2 merged → 2 kept (0 chat-only) · Persona: neutral (none) · Language: ASD-STE100, G8 clean\n"
    chk("CODEBASE footer starting with Level: is skipped cleanly", not ste_lint.lint(cb_legacy_footer), ste_lint.lint(cb_legacy_footer))

    skim_ctx = "**Context:** head `abc1234` · CI passing · size 12 changed lines · PR description ok."
    chk("skim CI passing/failing allowed on Context line", not ste_lint.lint(skim_ctx) and not ste_lint.lint(skim_ctx.replace("passing", "failing")), ste_lint.lint(skim_ctx))

    # --- 4. -ing, semicolons, hedges, markdown stripping ---
    chk("being is flagged as ing-form", "ing-form" in rules_of("This record is being deleted."), rules_of("This record is being deleted."))
    chk("allowed plurals, meaning, and finding pass", not ste_lint.lint("The meaning of these mappings and warnings matches the settings."), ste_lint.lint("The meaning of these mappings and warnings matches the settings."))
    earlier_cleared = "### Earlier findings\n- Earlier finding D1 cleared: `app/e.py:12` adds guard.\n"
    chk("Earlier finding cleared boilerplate passes", not ste_lint.lint(earlier_cleared), ste_lint.lint(earlier_cleared))
    chk("-ing in ### heading is flagged", "ing-form" in rules_of("### Breaking changes and updating handlers"), rules_of("### Breaking changes and updating handlers"))
    chk("semicolon flagged", "semicolon" in rules_of("Keep this small; split it."), rules_of("Keep this small; split it."))
    chk("curly apostrophe hedge flagged", "hedge-or-chat-abbrev" in rules_of("I’m not sure if this works."), rules_of("I’m not sure if this works."))

    single_line_fence = "```json```\nThis line has a semicolon; and maybe it fails."
    chk("single-line fence does not silence rest of document", {"semicolon", "hedge-or-chat-abbrev"}.issubset(set(rules_of(single_line_fence))), rules_of(single_line_fence))

    tilde_and_double = "~~~\nrunning; I think;\n~~~\nUse `` `running` `` in this config."
    chk("tilde fence and double-backtick span ignored", not ste_lint.lint(tilde_and_double), ste_lint.lint(tilde_and_double))

    url_period = (
        "The specification explains the retry rule for failed requests at https://example.com/docs. "
        "The worker reads the retry count from the header before it sends the next request."
    )
    chk("URL preserves trailing period for sentence split", not ste_lint.lint(url_period), ste_lint.lint(url_period))
    chk("URL preserves trailing question mark", not ste_lint.lint("AC3 sets the retry limit. Does AC3 match https://example.com/docs?"), ste_lint.lint("AC3 sets the retry limit. Does AC3 match https://example.com/docs?"))

    quote_period = (
        'The specification says "users can edit drafts only after they hold explicit write access." '
        'The worker checks this permission before it saves the record to the database table.'
    )
    chk("American quote preserves trailing period for sentence split", not ste_lint.lint(quote_period), ste_lint.lint(quote_period))

    link_paren = "See (app/models.py:42) and [docs/api.md](https://example.com/api.md)."
    chk("PATHLIKE and markdown links preserve surrounding syntax cleanly", not ste_lint.lint(link_paren), ste_lint.lint(link_paren))

    # --- 5. --findings handling ---
    chk("locations=None does not crash", not ste_lint.lint("See app/a.py:1.", findings=[{"id": "H1", "location": "app/a.py:1", "locations": None}]), ste_lint.lint("See app/a.py:1.", findings=[{"id": "H1", "location": "app/a.py:1", "locations": None}]))

    chk("line boundary 10 vs 100 catches evidence-lost", "evidence-lost" in rules_of("See app/a.py:100.", findings=[{"id": "H1", "location": "app/a.py:10"}]), rules_of("See app/a.py:100.", findings=[{"id": "H1", "location": "app/a.py:10"}]))
    chk("range app/a.py:10-15 satisfies app/a.py:10", not ste_lint.lint("See app/a.py:10-15.", findings=[{"id": "H1", "location": "app/a.py:10"}]), ste_lint.lint("See app/a.py:10-15.", findings=[{"id": "H1", "location": "app/a.py:10"}]))

    findings_mixed = {
        "findings": [
            {"id": "B1", "location": "head/app/a.py:10", "status": "todo", "eval_disposition": "post"},
            {"id": "B2", "location": "app/b.py:20", "status": "fixed"},
            {"id": "B3", "location": "app/c.py:30", "status": "declined"},
            {"id": "B4", "location": "app/d.py:40", "status": "todo", "eval_disposition": "chat-only"},
            {"id": "B5", "location": "app/e.py:50", "verified": "refuted"},
        ]
    }
    chk("findings.json filters fixed/declined/chat-only/refuted", not ste_lint.lint("See app/a.py:10.", findings=findings_mixed), ste_lint.lint("See app/a.py:10.", findings=findings_mixed))

    pareto_doc = {"shown": [{"id": "S1", "location": "app/sec.py:12"}], "cut_titles": ["Low nit"]}
    chk("pareto.json shown key supported", not ste_lint.lint("See app/sec.py:12.", findings=pareto_doc) and "evidence-lost" in rules_of("Missing.", findings=pareto_doc), rules_of("Missing.", findings=pareto_doc))

    legacy_hunk = [
        {"id": "X1", "file": "app/legacy.py", "lines": "15-18"},
        {"id": "X2", "location": "app/hunk.py @@ -1,2 +42,3 @@"},
    ]
    chk("legacy file/lines and HUNK_LOC supported", not ste_lint.lint("See app/legacy.py:15 and app/hunk.py:42.", findings=legacy_hunk), ste_lint.lint("See app/legacy.py:15 and app/hunk.py:42.", findings=legacy_hunk))

    # --- 6. CLI flags & exit codes ---
    with tempfile.TemporaryDirectory() as td:
        rep = os.path.join(td, "report.md")
        bad_rep = os.path.join(td, "bad.md")
        fjson = os.path.join(td, "reported.json")
        corrupt = os.path.join(td, "corrupt.json")
        with open(rep, "w", encoding="utf-8") as fh:
            fh.write("See `app/a.py:10`.\n")
        with open(bad_rep, "w", encoding="utf-8") as fh:
            fh.write("See nothing; maybe.\n")
        with open(fjson, "w", encoding="utf-8") as fh:
            json.dump([{"id": "C1", "location": "app/a.py:10"}], fh)
        with open(corrupt, "w", encoding="utf-8") as fh:
            fh.write("{bad json")

        rc0, out0, _ = run_cli(["--json", f"--findings={fjson}", "--level=CODEBASE", rep])
        chk("CLI flags before file + --findings=path + --level=CODEBASE exit 0", rc0 == 0 and json.loads(out0) == [], (rc0, out0))

        rc1, out1, _ = run_cli(["--json", bad_rep, "--findings", fjson])
        parsed1 = json.loads(out1)
        chk("CLI exit 1 with JSON issues", rc1 == 1 and {x["rule"] for x in parsed1} == {"semicolon", "hedge-or-chat-abbrev", "evidence-lost"}, (rc1, parsed1))

        rc2_missing, _, err2 = run_cli([os.path.join(td, "nope.md")])
        chk("CLI exit 2 on missing report file", rc2_missing == 2 and "error reading" in err2, (rc2_missing, err2))

        rc2_json, _, err2j = run_cli([rep, "--findings", corrupt])
        chk("CLI exit 2 on corrupt findings JSON", rc2_json == 2 and "error reading findings" in err2j, (rc2_json, err2j))

        rc2_arg, _, _ = run_cli([rep, "--findings"])
        chk("CLI exit 2 on missing --findings value", rc2_arg == 2, rc2_arg)

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
