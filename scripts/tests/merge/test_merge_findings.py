#!/usr/bin/env python3
"""Regression tests for scripts/merge_findings.py (merge + finalize). Run: python3 -I -B test_merge_findings.py"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "merge_findings.py"))
spec = importlib.util.spec_from_file_location("merge_findings", SCRIPT)
mf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mf)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)


def make_f(fid, lane, sev, loc, title, issue, **kw):
    f = {
        "id": fid, "lane": lane, "severity": sev, "confidence": "HIGH",
        "title": title, "location": loc, "issue": issue, "fix": "Fix the bug.",
        "trigger": "Input X produces wrong output Y." if sev in ("CRITICAL", "HIGH") else "n/a",
    }
    if sev == "CRITICAL":
        f["code_example"] = "safe_call(x)"
    f.update(kw)
    return f


def run_tests():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    with tempfile.TemporaryDirectory() as td:
        dmap = {"a.py": {"added": [10, 11, 12, 20, 30, 40], "context": [9, 13]}, "b.py": {"added": [5, 6], "context": [4]}}
        write_json(os.path.join(td, "diff-map.json"), dmap)
        write_json(os.path.join(td, "existing-comments.json"), {
            "inline": [{"path": "a.py", "line": 31, "body": "Duplicate null check missing on response payload parsing"}],
            "reviews": [],
        })
        write_json(os.path.join(td, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 7, "head_sha": "a" * 40, "author_is_me": False},
            "lane_plan": {"folded": []},
            "rules_changed": [],
        })

        # Raw lanes including fenced JSON string, duplicates within +-3 lines, G2 out-of-diff, G2 mixed locations, G4 drop, G5 match
        raw_lanes = {
            "security": "```json\n" + json.dumps({
                "lane": "security",
                "findings": [
                    make_f("sec-1", "security", "CRITICAL", "head/a.py:10", "SQL injection in query", "Unsanitized user input concatenated into SQL query."),
                    make_f("sec-2", "security", "HIGH", "a.py:99", "Out of diff issue", "Pre-existing code outside diff."),
                ],
                "safety_alerts": ["other.py:12 [injection]"],
            }) + "\n```",
            "code-quality": {
                "lane": "code-quality",
                "findings": [
                    # Duplicate of sec-1 within +-3 lines with overlapping words -> should merge into sec-1 keeping CRITICAL
                    make_f("cq-1", "code-quality", "HIGH", "a.py:11", "SQL query injection bug", "Unsanitized user input concatenated into SQL query string."),
                    # Mixed locations: a.py:99 pruned, a.py:20 kept
                    make_f("cq-2", "code-quality", "HIGH", "a.py:99", "Verbose error branch", "Error branch swallows timeout exception.", locations=["a.py:20", "z.py:1"]),
                    # Matches existing inline comment within +-5 lines (G5) -> dropped
                    make_f("cq-3", "code-quality", "MEDIUM", "a.py:30", "Missing null check", "Null check missing on response payload parsing here."),
                    # G4 LOW confidence on MEDIUM -> dropped
                    make_f("cq-4", "code-quality", "MEDIUM", "a.py:40", "Weak suspicion", "Maybe slow.", confidence="LOW"),
                    # Valid non-blocking MEDIUM & LOW -> go to nonblocking-candidates.json
                    make_f("cq-5", "code-quality", "MEDIUM", "b.py:5", "Unbounded retry loop", "Retry loop has no max attempt limit."),
                    make_f("cq-6", "code-quality", "LOW", "b.py:6", "Minor naming nit", "Helper name could be clearer."),
                ],
            },
            "adherence": {
                "lane": "adherence",
                "findings": [],
                "ac_matrix": [{"ref": "AC1", "text": "Sanitize input", "status": "Met", "evidence": "a.py:10", "test": "tests/t.py:5"}],
            },
        }
        write_json(os.path.join(td, "raw-lanes.json"), raw_lanes)

        # Capture merge output
        import io
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = mf.main(["merge", td])
        finally:
            sys.stdout = old_stdout
        m_out = json.loads(buf.getvalue())

        chk("merge exits 0", rc == 0, rc)
        chk("G2 drops out-of-diff sec-2", m_out["gate_counts"]["g2_out_of_diff"] == 1, m_out["gate_counts"])
        chk("G4 drops low-confidence cq-4", m_out["gate_counts"]["g4_low_conf"] == 1, m_out["gate_counts"])
        chk("G5 drops already-raised cq-3", m_out["gate_counts"]["g5_already_raised"] == 1, m_out["gate_counts"])
        chk("dedupe merges cq-1 into sec-1 (4 surviving: sec-1, cq-2, cq-5, cq-6)",
            m_out["merged_count"] == 4 and m_out["blocking_count"] == 2 and m_out["nonblocking_count"] == 2, m_out)
        chk("safety_alerts collected", m_out["safety_alerts"] == ["other.py:12 [injection]"], m_out["safety_alerts"])

        pre_v = json.load(open(os.path.join(td, "merged-pre-verify.json")))
        by_id = {f["id"]: f for f in pre_v["findings"]}
        chk("head/ prefix stripped on sec-1", by_id["sec-1"]["location"] == "a.py:10", by_id["sec-1"])
        chk("mixed locations pruned to a.py:20 on cq-2",
            by_id["cq-2"]["location"] == "a.py:20" and "locations" not in by_id["cq-2"], by_id["cq-2"])
        chk("merged sources include security and code-quality",
            set(by_id["sec-1"]["sources"]) == {"security", "code-quality"}, by_id["sec-1"]["sources"])

        batches = json.load(open(os.path.join(td, "verify-batches.json")))
        chk("verify batches group a.py blocking claims into 1 standard-tier batch",
            len(batches) == 1 and len(batches[0]["claims"]) == 2 and batches[0]["tier"] == "standard", batches)

        nb = json.load(open(os.path.join(td, "nonblocking-candidates.json")))
        chk("nonblocking-candidates holds cq-5 and cq-6 only",
            {f["id"] for f in nb["findings"]} == {"cq-5", "cq-6"}, nb["findings"])

        # Finalize: verifiers confirm sec-1 & cq-2; eval attempts illegal drop on sec-1, marks cq-5 post, cq-6 drop
        write_json(os.path.join(td, "verifier-results.json"), [
            {"id": "sec-1", "verdict": "confirmed", "reason": "Direct string concat at a.py:10."},
            {"id": "cq-2", "verdict": "confirmed", "reason": "Swallows timeout at a.py:20."},
        ])
        write_json(os.path.join(td, "eval-results.json"), {
            "evaluations": [
                {"id": "sec-1", "disposition": "drop", "eval_reason": "Try illegal drop on CRITICAL."},
                {"id": "cq-5", "disposition": "chat-only", "eval_reason": "Informational retry note."},
                {"id": "cq-6", "disposition": "drop", "eval_reason": "Minor naming nitpick."},
            ]
        })

        buf2 = io.StringIO()
        sys.stdout = buf2
        try:
            rc2 = mf.main(["finalize", td])
        finally:
            sys.stdout = old_stdout
        f_out = json.loads(buf2.getvalue())

        chk("finalize exits 0 and breaker valid", rc2 == 0 and f_out["valid"], f_out)
        chk("safety floor overrides illegal drop on confirmed CRITICAL sec-1",
            len(f_out["floor_overrides"]) == 1 and "sec-1" in f_out["floor_overrides"][0], f_out["floor_overrides"])
        chk("verdict is REQUEST CHANGES due to confirmed CRITICAL",
            f_out["verdict"] == "REQUEST CHANGES" and f_out["event"] == "REQUEST_CHANGES", f_out)
        chk("eval routes cq-5 to chat-only and drops cq-6",
            f_out["counts"]["chat_only"] == 1 and f_out["counts"]["dropped_by_eval"] == 1 and f_out["counts"]["post"] == 2,
            f_out["counts"])
        chk("non-floor confirmed HIGH cq-2 flagged in eval_followup_needed",
            len(f_out["eval_followup_needed"]) == 1 and f_out["eval_followup_needed"][0]["id"] == "cq-2",
            f_out["eval_followup_needed"])

        # Test medium Pareto cut & zero_findings_fast_path
        td2 = os.path.join(td, "clean")
        os.makedirs(td2)
        write_json(os.path.join(td2, "diff-map.json"), dmap)
        write_json(os.path.join(td2, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td2, "bundle-summary.json"), {
            "effort": {"resolved": "medium"},
            "pr": {"owner": "octo", "repo": "demo", "number": 8, "head_sha": "b" * 40, "author_is_me": False},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td2, "raw-lanes.json"), {
            "adherence": {"lane": "adherence", "findings": [], "ac_matrix": []},
            "code-quality": {"lane": "code-quality", "findings": []},
        })
        buf3 = io.StringIO()
        sys.stdout = buf3
        try:
            mf.main(["merge", td2])
            buf4 = io.StringIO()
            sys.stdout = buf4
            rc4 = mf.main(["finalize", td2, "--effort", "medium"])
        finally:
            sys.stdout = old_stdout
        f_clean = json.loads(buf4.getvalue())
        chk("zero-findings fast path sets zero_findings_fast_path=True and APPROVE",
            rc4 == 0 and f_clean["zero_findings_fast_path"] and f_clean["verdict"] == "APPROVE", f_clean)

        # Test medium Pareto cut keeps verified HIGH and caps tail LOW items
        td3 = os.path.join(td, "pareto")
        os.makedirs(td3)
        dmap3 = {"p.py": {"added": list(range(1, 30)), "context": []}}
        write_json(os.path.join(td3, "diff-map.json"), dmap3)
        write_json(os.path.join(td3, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td3, "bundle-summary.json"), {
            "effort": {"resolved": "medium"},
            "pr": {"owner": "octo", "repo": "demo", "number": 9, "head_sha": "c" * 40, "author_is_me": False},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        p_findings = [make_f("h-1", "security", "HIGH", "p.py:1", "Auth bypass", "Missing tenant check on p.py:1.")]
        for idx in range(2, 18):
            p_findings.append(make_f(f"l-{idx}", "code-quality", "LOW", f"p.py:{idx}", f"Minor nit {idx}", f"Small style note {idx}.", confidence="MEDIUM"))
        write_json(os.path.join(td3, "raw-lanes.json"), {"security": {"lane": "security", "findings": [p_findings[0]]}, "code-quality": {"lane": "code-quality", "findings": p_findings[1:]}})
        write_json(os.path.join(td3, "verifier-results.json"), [{"id": "h-1", "verdict": "confirmed", "reason": "Confirmed."}])
        buf5 = io.StringIO()
        sys.stdout = buf5
        try:
            mf.main(["merge", td3, "--effort", "medium"])
            buf6 = io.StringIO()
            sys.stdout = buf6
            rc6 = mf.main(["finalize", td3, "--effort", "medium"])
        finally:
            sys.stdout = old_stdout
        f_par = json.loads(buf6.getvalue())
        chk("medium Pareto cut produces pareto.json and trims tail LOW items",
            rc6 == 0 and f_par["pareto"] is not None and f_par["pareto"]["shown"] < f_par["pareto"]["total"]
            and os.path.isfile(os.path.join(td3, "pareto.json")), f_par["pareto"])

        # Test CODEBASE level end-to-end: file-map.json G2, sharded lane aggregation, HEALTHY fast path, NEEDS ACTION
        td_cb = os.path.join(td, "codebase")
        os.makedirs(td_cb)
        write_json(os.path.join(td_cb, "file-map.json"), {
            "src/auth.py": {"lines": 40, "lang": "python"},
            "src/db.py": {"lines": 30, "lang": "python"},
        })
        write_json(os.path.join(td_cb, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_cb, "bundle-summary.json"), {
            "level": "CODEBASE",
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 0, "head_sha": "d" * 40, "author_is_me": True},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_cb, "raw-lanes.json"), [
            {
                "lane": "security",
                "findings": [
                    make_f("s-1", "security", "CRITICAL", "src/auth.py:12", "Missing tenant scope", "Query omits tenant filter.", introduced_by_pr="codebase"),
                    make_f("s-oob", "security", "HIGH", "src/auth.py:999", "Out of bounds line", "Line past EOF.", introduced_by_pr="codebase"),
                ],
            },
            {
                "lane": "code-quality",
                "shard": "code-quality-shard-1",
                "findings": [
                    make_f("cq-s1", "code-quality", "MEDIUM", "src/db.py:10", "Unclosed cursor", "Cursor leaks on exception.", introduced_by_pr="codebase"),
                ],
            },
            {
                "lane": "code-quality",
                "shard": "code-quality-shard-2",
                "status": "NOT ASSESSED",
                "reason": "timeout on shard 2",
                "findings": [],
            },
        ])
        buf_cbm = io.StringIO()
        sys.stdout = buf_cbm
        try:
            rc_cbm = mf.main(["merge", td_cb])
        finally:
            sys.stdout = old_stdout
        cb_m = json.loads(buf_cbm.getvalue())
        cb_pre = json.load(open(os.path.join(td_cb, "merged-pre-verify.json")))
        cq_lane_row = next(ln for ln in cb_pre["lanes"] if ln["lane"] == "code-quality")
        chk("CODEBASE merge validates against file-map.json and drops line past EOF",
            rc_cbm == 0 and cb_m["level"] == "CODEBASE" and cb_m["gate_counts"]["g2_out_of_diff"] == 1
            and cb_m["merged_count"] == 2, cb_m)
        chk("CODEBASE merge aggregates sharded lane failure to NOT ASSESSED",
            cq_lane_row["status"] == "NOT ASSESSED" and "shard-2" in cq_lane_row["reason"], cq_lane_row)

        write_json(os.path.join(td_cb, "verifier-results.json"), [
            {"id": "s-1", "verdict": "confirmed", "reason": "Confirmed at src/auth.py:12."},
        ])
        write_json(os.path.join(td_cb, "eval-results.json"), {
            "evaluations": [{"id": "cq-s1", "disposition": "post", "eval_reason": "Real resource leak."}],
        })
        buf_cbf = io.StringIO()
        sys.stdout = buf_cbf
        try:
            rc_cbf = mf.main(["finalize", td_cb])
        finally:
            sys.stdout = old_stdout
        cb_f = json.loads(buf_cbf.getvalue())
        chk("CODEBASE finalize returns NEEDS ACTION, event NONE, and incomplete flag from shard failure",
            rc_cbf == 0 and cb_f["level"] == "CODEBASE" and cb_f["verdict"] == "NEEDS ACTION"
            and cb_f["event"] == "NONE" and not cb_f["complete"], cb_f)

        # Clean CODEBASE review -> HEALTHY + zero_findings_fast_path
        td_cb2 = os.path.join(td, "codebase-clean")
        os.makedirs(td_cb2)
        write_json(os.path.join(td_cb2, "file-map.json"), {"src/auth.py": {"lines": 40, "lang": "python"}})
        write_json(os.path.join(td_cb2, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_cb2, "bundle-summary.json"), {
            "level": "CODEBASE", "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 0, "head_sha": "e" * 40, "author_is_me": True},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_cb2, "raw-lanes.json"), [
            {"lane": "adherence", "findings": [], "ac_matrix": []},
            {"lane": "code-quality", "findings": []},
        ])
        buf_cb2 = io.StringIO()
        sys.stdout = buf_cb2
        try:
            mf.main(["merge", td_cb2])
            buf_cb2f = io.StringIO()
            sys.stdout = buf_cb2f
            rc_cb2f = mf.main(["finalize", td_cb2])
        finally:
            sys.stdout = old_stdout
        cb2_f = json.loads(buf_cb2f.getvalue())
        chk("CODEBASE zero-findings sets HEALTHY, event NONE, zero_findings_fast_path=True",
            rc_cb2f == 0 and cb2_f["verdict"] == "HEALTHY" and cb2_f["event"] == "NONE" and cb2_f["zero_findings_fast_path"],
            cb2_f)

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
