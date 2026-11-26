#!/usr/bin/env python3
"""Regression tests for scripts/merge_findings.py (merge, drop-g6, finalize). Run: python3 -I -B test_merge_findings.py"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "merge_findings.py"))
BREAKER_SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "breaker.py"))
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


def run_mf(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mf.main(argv)
    return rc, out.getvalue(), err.getvalue()


def run_tests():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, cond, detail))

    with tempfile.TemporaryDirectory() as td:
        dmap = {"a.py": {"added": [10, 11, 12, 20, 30, 40], "context": [9, 13]}, "b.py": {"added": [5, 6], "context": [4]}}
        write_json(os.path.join(td, "diff-map.json"), dmap)
        write_json(os.path.join(td, "existing-comments.json"), {
            "inline": [{"path": "a.py", "line": 31, "body": "Duplicate null check missing on response payload parsing"}],
            "reviews": [{"state": "COMMENTED", "body": "In b.py: Avoid swallowing timeout error in helper loop."}],
        })
        write_json(os.path.join(td, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 7, "head_sha": "a" * 40, "author_is_me": False},
            "lane_plan": {"folded": []},
            "rules_changed": [{"path": "CLAUDE.md", "status": "modified"}],
        })

        # Raw lanes: fenced JSON with nested ```suggestion inside code_example + prose block before it
        raw_lanes = {
            "security": "Preliminary note:\n```python\nx = 1\n```\n```JSON\n" + json.dumps({
                "lane": "security",
                "findings": [
                    make_f("sec-1", "security", "CRITICAL", "head/a.py:10", "SQL injection in query",
                           "Unsanitized user input concatenated into SQL query.",
                           code_example="```suggestion\ncur.execute(sql, (val,))\n```"),
                    make_f("sec-2", "security", "HIGH", "a.py:99", "Out of diff issue", "Pre-existing code outside diff."),
                ],
                "safety_alerts": ["other.py:12 [injection]"],
            }) + "\n```",
            "code-quality": {
                "lane": "code-quality",
                "findings": [
                    make_f("cq-1", "code-quality", "HIGH", "a.py:11", "SQL query injection bug", "Unsanitized user input concatenated into SQL query string."),
                    make_f("cq-2", "code-quality", "HIGH", "a.py:99", "Verbose error branch", "Error branch swallows timeout exception.", locations=["a.py:20", "z.py:1"]),
                    make_f("cq-3", "code-quality", "MEDIUM", "a.py:30", "Missing null check", "Null check missing on response payload parsing here."),
                    make_f("cq-rev", "code-quality", "MEDIUM", "b.py:6", "Swallowing timeout error", "Avoid swallowing timeout error in helper loop."),
                    make_f("cq-4", "code-quality", "MEDIUM", "a.py:40", "Weak suspicion", "Maybe slow.", confidence="LOW"),
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

        rc, out_s, _ = run_mf(["merge", td])
        m_out = json.loads(out_s)
        chk("merge exits 0", rc == 0, rc)
        chk("G2 drops out-of-diff sec-2", m_out["gate_counts"]["g2_out_of_diff"] == 1, m_out["gate_counts"])
        chk("G4 drops low-confidence cq-4", m_out["gate_counts"]["g4_low_conf"] == 1, m_out["gate_counts"])
        chk("G5 drops already-raised cq-3 (inline) and cq-rev (review body)", m_out["gate_counts"]["g5_already_raised"] == 2, m_out["gate_counts"])
        chk("dedupe merges cq-1 into sec-1 (4 surviving: sec-1, cq-2, cq-5, cq-6)",
            m_out["merged_count"] == 4 and m_out["blocking_count"] == 2 and m_out["nonblocking_count"] == 2, m_out)
        chk("safety_alerts collected", m_out["safety_alerts"] == ["other.py:12 [injection]"], m_out["safety_alerts"])
        chk("rules_changed adds rule files edited to high_risk", "rule files edited" in m_out["high_risk"], m_out["high_risk"])

        pre_v = json.load(open(os.path.join(td, "merged-pre-verify.json")))
        by_id = {f["id"]: f for f in pre_v["findings"]}
        chk("head/ prefix stripped on sec-1", by_id["sec-1"]["location"] == "a.py:10", by_id["sec-1"])
        chk("nested fenced suggestion inside code_example preserved", "```suggestion" in by_id["sec-1"]["code_example"], by_id["sec-1"])
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

        # Finalize: verifiers confirm sec-1 & cq-2; eval attempts illegal drop on sec-1, marks cq-5 chat-only, cq-6 drop
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

        rc2, out_s2, _ = run_mf(["finalize", td])
        f_out = json.loads(out_s2)
        chk("finalize exits 0 and breaker valid", rc2 == 0 and f_out["valid"], f_out)
        chk("finalize counts include merged", f_out["counts"]["merged"] == 4, f_out["counts"])
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
        # Check non-carry PII redaction and sharded ac_matrix dedupe separately
        td_pii = os.path.join(td, "noncarry-pii")
        os.makedirs(td_pii)
        write_json(os.path.join(td_pii, "diff-map.json"), dmap)
        write_json(os.path.join(td_pii, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_pii, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 71, "head_sha": "a" * 40},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_pii, "raw-lanes.json"), {
            "adherence-shard-1": {
                "findings": [make_f("ad-1", "adherence", "MEDIUM", "a.py:10", "Note by @alice", "Ask bob@example.com.", ac_ref="AC1")],
                "ac_matrix": [{"ref": "AC1", "text": "Sanitize", "status": "Met", "evidence": "a.py:10", "test": "untested"}],
            },
            "adherence-shard-2": {
                "findings": [],
                "ac_matrix": [{"ref": "AC1", "text": "Sanitize", "status": "Partial", "evidence": "—", "test": "t.py:1", "note": "missing branch"}],
            },
        })
        run_mf(["merge", td_pii])
        rc_pii, _, _ = run_mf(["finalize", td_pii])
        pii_findings = json.load(open(os.path.join(td_pii, "findings.json")))
        pii_merged = json.load(open(os.path.join(td_pii, "merged.json")))
        chk("non-carry finalize redacts PII in findings.json & deduplicates sharded ac_matrix to Partial",
            rc_pii == 0 and "@alice" not in json.dumps(pii_findings) and "bob@example.com" not in json.dumps(pii_findings)
            and len(pii_merged["ac_matrix"]) == 1 and pii_merged["ac_matrix"][0]["status"] == "Partial",
            (pii_findings, pii_merged["ac_matrix"]))
        chk("zero_findings_fast_path is False when safety_alerts present",
            not f_out["zero_findings_fast_path"], f_out)

        # Standalone replay of breaker-input.json without CLI option flags
        p_replay = subprocess.run(
            [sys.executable, "-I", "-B", BREAKER_SCRIPT, os.path.join(td, "breaker-input.json")],
            capture_output=True, text=True,
        )
        r_rep = json.loads(p_replay.stdout)
        chk("breaker-input.json replays standalone with identical verdict/event",
            p_replay.returncode == 0 and r_rep["verdict"] == f_out["verdict"] and r_rep["event"] == f_out["event"], r_rep)

        # Test zero_findings_fast_path False when safety_alerts present even with 0 findings
        td_sa = os.path.join(td, "safety-only")
        os.makedirs(td_sa)
        write_json(os.path.join(td_sa, "diff-map.json"), dmap)
        write_json(os.path.join(td_sa, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_sa, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 70, "head_sha": "a" * 40},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_sa, "raw-lanes.json"), {
            "adherence": {"lane": "adherence", "findings": [], "safety_alerts": "x.py:1 [prompt injection]"},
        })
        run_mf(["merge", td_sa])
        rc_sa, out_sa, _ = run_mf(["finalize", td_sa])
        sa_f = json.loads(out_sa)
        chk("zero_findings_fast_path=False when safety_alerts non-empty",
            rc_sa == 0 and not sa_f["zero_findings_fast_path"] and sa_f["safety_alerts"] == ["x.py:1 [prompt injection]"], sa_f)

        # Test clean medium fast path
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
        run_mf(["merge", td2])
        rc4, out_s4, _ = run_mf(["finalize", td2, "--effort", "medium"])
        f_clean = json.loads(out_s4)
        chk("zero-findings fast path sets zero_findings_fast_path=True and APPROVE",
            rc4 == 0 and f_clean["zero_findings_fast_path"] and f_clean["verdict"] == "APPROVE", f_clean)

        # Test medium Pareto cut & emphasis tie-breaking
        td3 = os.path.join(td, "pareto")
        os.makedirs(td3)
        dmap3 = {"p.py": {"added": list(range(1, 120)), "context": []}}
        write_json(os.path.join(td3, "diff-map.json"), dmap3)
        write_json(os.path.join(td3, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td3, "bundle-summary.json"), {
            "effort": {"resolved": "medium"},
            "pr": {"owner": "octo", "repo": "demo", "number": 9, "head_sha": "c" * 40, "author_is_me": False},
            "persona": {"emphasis": ["performance"]},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        p_findings = [make_f("h-1", "security", "HIGH", "p.py:1", "Auth bypass", "Missing tenant check on p.py:1.")]
        for idx in range(2, 18):
            lane = "performance" if idx == 17 else "code-quality"
            p_findings.append(make_f(
                f"l-{idx}", lane, "LOW", f"p.py:{idx * 5}",
                f"Check detail alpha_{idx}", f"Observation beta_{idx} on line {idx * 5}.", confidence="MEDIUM",
            ))
        write_json(os.path.join(td3, "raw-lanes.json"), {
            "security": {"lane": "security", "findings": [p_findings[0]]},
            "code-quality": {"lane": "code-quality", "findings": p_findings[1:-1]},
            "performance": {"lane": "performance", "findings": [p_findings[-1]]},
        })
        write_json(os.path.join(td3, "verifier-results.json"), [{"id": "h-1", "verdict": "confirmed", "reason": "Confirmed."}])
        run_mf(["merge", td3, "--effort", "medium"])
        rc6, out_s6, _ = run_mf(["finalize", td3, "--effort", "medium"])
        f_par = json.loads(out_s6)
        par_doc = json.load(open(os.path.join(td3, "pareto.json")))
        chk("medium Pareto cut produces pareto.json and trims tail LOW items",
            rc6 == 0 and f_par["pareto"] is not None and f_par["pareto"]["shown"] < f_par["pareto"]["total"]
            and os.path.isfile(os.path.join(td3, "pareto.json")), f_par["pareto"])
        chk("pareto_cut breaks ties using persona.emphasis (l-17 performance ahead of l-2 code-quality)",
            [x["id"] for x in par_doc["shown"][:2]] == ["h-1", "l-17"], [x["id"] for x in par_doc["shown"][:3]])

        # Test drop-g6 subcommand (preserves blocking, drops non-blocking)
        rc_g6, out_g6, _ = run_mf(["drop-g6", td3, "--ids", "h-1,l-2,l-3"])
        g6_doc = json.loads(out_g6)
        chk("drop-g6 removes non-blocking l-2/l-3 and refuses blocking h-1",
            rc_g6 == 0 and set(g6_doc["dropped"]) == {"l-2", "l-3"} and g6_doc["skipped_blocking"] == ["h-1"]
            and g6_doc["gate_counts"]["g6_linter"] == 2, g6_doc)

        # Test skim effort keeps MEDIUM/LOW (effort never filters findings) & single-lane dict raw-lanes
        td_sk = os.path.join(td, "skim")
        os.makedirs(td_sk)
        write_json(os.path.join(td_sk, "diff-map.json"), dmap)
        write_json(os.path.join(td_sk, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_sk, "bundle-summary.json"), {
            "effort": {"resolved": "skim"},
            "pr": {"owner": "octo", "repo": "demo", "number": 10, "head_sha": "f" * 40, "ci": "passing"},
            "gate_g0b": {"in_skim_profile": True},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_sk, "raw-lanes.json"), {
            "lane": "skim",
            "findings": [
                make_f("sk-1", "skim", "MEDIUM", "a.py:10", "Direct medium item", "Medium item in skim."),
                make_f("sk-2", "skim", "HIGH", "a.py:11", "HIGH without trigger", "Missing trigger causes G3 -> MEDIUM.", trigger=""),
                make_f("sk-3", "skim", "QUESTION", "a.py:12", "Clarify edge case", "Is zero allowed here?"),
            ],
        })
        rc_skm, out_skm, _ = run_mf(["merge", td_sk, "--effort", "skim"])
        skm = json.loads(out_skm)
        rc_skf, out_skf, _ = run_mf(["finalize", td_sk, "--effort", "skim"])
        skf = json.loads(out_skf)
        chk("skim single-lane dict parsed & MEDIUM/G3-downgraded items are kept (effort never filters findings)",
            rc_skm == 0 and skm["gate_counts"]["skim_dropped"] == 0 and rc_skf == 0 and skf["valid"]
            and skf["verdict"] == "APPROVE WITH COMMENTS" and skf["counts"]["post"] == 3
            and skf["next_step"] == "document", (skm, skf))

        # Test dedupe field preservation, swapped locations[], INTENT_LOC dedupe, and HUNK_LOC support
        td_dd = os.path.join(td, "dedupe")
        os.makedirs(td_dd)
        write_json(os.path.join(td_dd, "diff-map.json"), dmap)
        write_json(os.path.join(td_dd, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_dd, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 11, "head_sha": "1" * 40},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_dd, "raw-lanes.json"), {
            "code-quality": {
                "lane": "code-quality",
                "findings": [
                    make_f("cq-swap", "code-quality", "HIGH", "b.py:5", "Missing auth token check",
                           "Handler fails to validate auth token before update.", confidence="MEDIUM", ac_ref="AC2"),
                    make_f("cq-hunk", "code-quality", "HIGH", "a.py @@ -9,2 +10,2 @@", "Hunk timeout bug",
                           "Hunk swallows timeout error on retry."),
                ],
            },
            "security": {
                "lane": "security",
                "findings": [
                    make_f("sec-swap", "security", "HIGH", "a.py:10", "Missing auth token check",
                           "Handler fails to validate auth token before update.", locations=["b.py:5"],
                           breaking={"consumers": "API", "migration": "none"}),
                ],
            },
            "adherence": {
                "lane": "adherence",
                "findings": [
                    make_f("ad-i1", "adherence", "HIGH", "intent.md §AC1", "AC1 missing auth check",
                           "Intent AC1 requires tenant auth check before save.", ac_ref="AC1"),
                ],
                "ac_matrix": [{"ref": "AC1", "text": "Tenant check", "status": "Missing", "evidence": "intent.md §AC1", "test": "untested"}],
            },
            "critical-risk": {
                "lane": "critical-risk",
                "findings": [
                    make_f("cr-i1", "critical-risk", "HIGH", "intent.md §AC1", "AC1 missing auth check",
                           "Intent AC1 requires tenant auth check before save."),
                ],
            },
        })
        rc_dd, out_dd, _ = run_mf(["merge", td_dd])
        dd_m = json.loads(out_dd)
        dd_pre = json.load(open(os.path.join(td_dd, "merged-pre-verify.json")))
        dd_batches = json.load(open(os.path.join(td_dd, "verify-batches.json")))
        swap_item = next(x for x in dd_pre["findings"] if x["id"] == "sec-swap")
        chk("dedupe picks security over code-quality at equal HIGH and preserves ac_ref/breaking/HIGH conf",
            dd_m["merged_count"] == 3 and swap_item["lane"] == "security" and "AC2" in swap_item["ac_ref"]
            and swap_item["breaking"]["migration"] == "none" and swap_item["confidence"] == "HIGH", swap_item)
        chk("INTENT_LOC duplicates deduped and HUNK_LOC batched under file a.py",
            any("a.py" in b["files"] and any(c["id"] == "cq-hunk" for c in b["claims"]) for b in dd_batches), dd_batches)

        # Test verifier severity-too-high on CRITICAL & HIGH, severity-too-low, unclear, and eval sole-AC protection
        write_json(os.path.join(td_dd, "verifier-results.json"), [
            {"id": "sec-swap", "verdict": "confirmed", "reason": "Confirmed.", "severity_note": "severity-too-low"},
            {"id": "cq-hunk", "verdict": "confirmed", "reason": "Lower.", "severity_note": "severity-too-high"},
            {"id": "ad-i1", "verdict": "unclear", "reason": "Need author check."},
        ])
        write_json(os.path.join(td_dd, "eval-results.json"), [
            {"id": "ad-i1", "disposition": "drop", "eval_reason": "Try dropping sole AC1 finding."},
        ])
        rc_ddf, out_ddf, _ = run_mf(["finalize", td_dd])
        ddf = json.loads(out_ddf)
        chk("verifier severity-too-low upgrades HIGH->CRITICAL, severity-too-high HIGH->MEDIUM routed to eval_followup_needed, sole AC1 drop overridden",
            rc_ddf == 0 and ddf["valid"] and ddf["open"]["critical"] == 1
            and any(x["id"] == "cq-hunk" for x in ddf["eval_followup_needed"])
            and any("AC1" in msg for msg in ddf["floor_overrides"]), (ddf, ddf["floor_overrides"]))

        # Test Carry Round 1 (no previous.json) + non-carry PII redaction + Carry Round 2 (cleared lead + open.json G2 exempt + B1 AC/finding separation)
        td_c1 = os.path.join(td, "carry-r1")
        os.makedirs(td_c1)
        write_json(os.path.join(td_c1, "diff-map.json"), dmap)
        write_json(os.path.join(td_c1, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_c1, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 12, "head_sha": "2" * 40},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_c1, "raw-lanes.json"), {
            "adherence": {
                "lane": "adherence",
                "findings": [make_f("ad-1", "adherence", "HIGH", "a.py:10", "Missing check per @alice", "Broken per owner@example.com.", ac_ref="B1")],
                "ac_matrix": [{"ref": "B1", "text": "Check input", "status": "Missing", "evidence": "a.py:10", "test": "untested"}],
            },
        })
        write_json(os.path.join(td_c1, "verifier-results.json"), [
            {"id": "ad-1", "verdict": "confirmed", "reason": "Confirmed."},
            {"id": "ac:B1", "verdict": "confirmed", "reason": "Confirmed."},
        ])
        run_mf(["merge", td_c1])
        rc_c1, out_c1, _ = run_mf(["finalize", td_c1, "--carry"])
        c1_f = json.loads(out_c1)
        c1_doc = json.load(open(os.path.join(td_c1, "findings.json")))
        chk("carry Round 1 without previous.json initializes stable id B1, next_id, status, origin, display_id and redacts PII",
            rc_c1 == 0 and c1_f["valid"] and c1_doc["next_id"] == {"B": 2}
            and c1_doc["findings"][0]["id"] == "B1" and c1_doc["findings"][0]["status"] == "todo"
            and c1_doc["findings"][0]["origin"] == "new" and c1_doc["findings"][0]["display_id"] == "H1"
            and "@alice" not in json.dumps(c1_doc) and "owner@example.com" not in json.dumps(c1_doc),
            (c1_f, c1_doc))

        # Round 2: B1 re-raised out-of-diff via open.json exemption + B1 AC refuted by ac:B1 without colliding + lead cleared S1
        td_c2 = os.path.join(td, "carry-r2")
        os.makedirs(td_c2)
        write_json(os.path.join(td_c2, "diff-map.json"), {"b.py": {"added": [5], "context": [4]}})
        write_json(os.path.join(td_c2, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_c2, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 12, "head_sha": "3" * 40},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_c2, "previous.json"), {
            "pr": "octo/demo#12",
            "last_sha": "2" * 40,
            "next_id": {"B": 2, "S": 2},
            "findings": [
                make_f("B1", "adherence", "HIGH", "a.py:10", "Missing check", "Broken check.", status="todo", origin="new", ac_ref="B1"),
                make_f("S1", "security", "HIGH", "a.py:20", "Token leak", "Token logged.", status="todo", origin="new"),
                make_f("D1", "code-quality", "LOW", "b.py:5", "Carried low", "Carried low note.", status="todo", origin="new"),
            ],
        })
        write_json(os.path.join(td_c2, "open.json"), [
            {"id": "B1", "lanes": ["adherence"], "file": "a.py", "lines": "10", "severity": "HIGH", "issue": "Broken check."},
            {"id": "S1", "lanes": ["security"], "file": "a.py", "lines": "20", "severity": "HIGH", "issue": "Token logged."},
        ])
        write_json(os.path.join(td_c2, "raw-lanes.json"), {
            "adherence": {
                "lane": "adherence",
                "findings": [make_f("B1", "adherence", "HIGH", "a.py:10", "Missing check", "Broken check.", ac_ref="B1")],
                "ac_matrix": [{"ref": "B1", "text": "Check input", "status": "Missing", "evidence": "a.py:10", "test": "untested"}],
            },
        })
        rc_c2m, out_c2m, _ = run_mf(["merge", td_c2])
        c2m = json.loads(out_c2m)
        batches_c2 = json.load(open(os.path.join(td_c2, "verify-batches.json")))
        chk("open.json re-raised B1 exempt from G2 out-of-diff & ac:B1 separated in verify-batches",
            rc_c2m == 0 and c2m["merged_count"] == 1
            and {c["id"] for b in batches_c2 for c in b["claims"]} == {"B1", "ac:B1"}, (c2m, batches_c2))

        write_json(os.path.join(td_c2, "verifier-results.json"), [
            {"id": "B1", "verdict": "confirmed", "reason": "Still broken at a.py:10."},
            {"id": "ac:B1", "verdict": "refuted", "reason": "Requirement B1 was actually met."},
        ])
        write_json(os.path.join(td_c2, "lead-cleared.json"), [
            {"id": "S1", "evidence": "a.py:20 fixed", "resolved": True, "lane": "lead"},
        ])
        write_json(os.path.join(td_c2, "still-open.json"), ["D1"])
        rc_c2f, out_c2f, err_c2f = run_mf([
            "finalize", td_c2, "--carry",
            "--cleared", os.path.join(td_c2, "lead-cleared.json"),
            "--still-open", os.path.join(td_c2, "still-open.json"),
        ])
        c2f = json.loads(out_c2f)
        c2_doc = json.load(open(os.path.join(td_c2, "findings.json")))
        chk("carry Round 2 with lane='lead' resolved clear & ac:B1 separation passes breaker cleanly",
            rc_c2f == 0 and c2f["valid"],
            (rc_c2f, c2f, err_c2f))
        chk("carried still_open LOW D1 preserved cleanly",
            c2f["open"]["low"] == 1 and any(x["id"] == "D1" and x["status"] == "todo" for x in c2_doc["findings"]), c2f)
        chk("B1 finding stays confirmed while ac:B1 refuted -> Met",
            any(x["id"] == "B1" and x["status"] == "todo" for x in c2_doc["findings"])
            and any(x["id"] == "S1" and x["status"] == "fixed" for x in c2_doc["findings"])
            and len(c2_doc["previous"]) == 3, c2_doc)

        # Carry with 0 new post findings + still_open LOW -> verdict APPROVE, zero_findings_fast_path=False
        td_cl = os.path.join(td, "carry-low-fastpath")
        os.makedirs(td_cl)
        write_json(os.path.join(td_cl, "diff-map.json"), {"b.py": {"added": [5], "context": [4]}})
        write_json(os.path.join(td_cl, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_cl, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 13, "head_sha": "4" * 40},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_cl, "previous.json"), {
            "pr": "octo/demo#13", "last_sha": "3" * 40, "next_id": {"D": 2},
            "findings": [make_f("D1", "code-quality", "LOW", "b.py:5", "Carried low", "Carried low note.", status="todo", origin="new")],
        })
        write_json(os.path.join(td_cl, "raw-lanes.json"), {"code-quality": {"lane": "code-quality", "findings": []}})
        write_json(os.path.join(td_cl, "still-open.json"), ["D1"])
        run_mf(["merge", td_cl])
        rc_clf, out_clf, _ = run_mf(["finalize", td_cl, "--carry", "--still-open", os.path.join(td_cl, "still-open.json")])
        clf = json.loads(out_clf)
        chk("zero_findings_fast_path=False when verdict APPROVE but carried still_open LOW exists",
            rc_clf == 0 and clf["valid"] and clf["verdict"] == "APPROVE"
            and clf["open"]["low"] == 1 and not clf["zero_findings_fast_path"], clf)

        # Test CODEBASE level end-to-end: file-map.json G2, sharded lane aggregation + colliding shard IDs, missing file-map exits 2
        td_cb_miss = os.path.join(td, "cb-missing-fmap")
        os.makedirs(td_cb_miss)
        write_json(os.path.join(td_cb_miss, "diff-map.json"), dmap)
        write_json(os.path.join(td_cb_miss, "bundle-summary.json"), {"level": "CODEBASE", "effort": {"resolved": "high"}})
        write_json(os.path.join(td_cb_miss, "raw-lanes.json"), [])
        rc_miss, _, err_miss = run_mf(["merge", td_cb_miss])
        chk("CODEBASE missing file-map.json exits 2 without falling back to diff-map.json",
            rc_miss == 2 and "file-map.json" in err_miss, (rc_miss, err_miss))

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
        write_json(os.path.join(td_cb, "raw-lanes.json"), {
            "security": {
                "lane": "security",
                "findings": [
                    make_f("s-1", "security", "CRITICAL", "src/auth.py:12", "Missing tenant scope", "Query omits tenant filter.", introduced_by_pr="codebase"),
                    make_f("s-oob", "security", "HIGH", "src/auth.py:999", "Out of bounds line", "Line past EOF.", introduced_by_pr="codebase"),
                ],
            },
            "code-quality-shard-1": {
                "findings": [
                    make_f("cq-1", "code-quality", "MEDIUM", "src/db.py:10", "Unclosed cursor", "Cursor leaks on exception.", introduced_by_pr="codebase"),
                ],
            },
            "code-quality-shard-2": {
                "status": "NOT ASSESSED",
                "reason": "timeout on shard 2",
                "findings": [
                    # even if a returned shard emitted colliding cq-1, test shard-3 below too
                ],
            },
            "code-quality-shard-3": {
                "findings": [
                    make_f("cq-1", "code-quality", "MEDIUM", "src/auth.py:25", "Deep nesting", "Refactor nested loops.", introduced_by_pr="codebase"),
                ],
            },
        })
        rc_cbm, out_cbm, _ = run_mf(["merge", td_cb])
        cb_m = json.loads(out_cbm)
        cb_pre = json.load(open(os.path.join(td_cb, "merged-pre-verify.json")))
        cq_lane_row = next(ln for ln in cb_pre["lanes"] if ln["lane"] == "code-quality")
        cb_ids = [x["id"] for x in cb_pre["findings"]]
        chk("CODEBASE merge validates against file-map.json and drops line past EOF",
            rc_cbm == 0 and cb_m["level"] == "CODEBASE" and cb_m["gate_counts"]["g2_out_of_diff"] == 1
            and cb_m["merged_count"] == 3, cb_m)
        chk("CODEBASE colliding shard IDs disambiguated uniquely",
            len(cb_ids) == len(set(cb_ids)) and len(cb_ids) == 3, cb_ids)
        chk("CODEBASE merge aggregates sharded lane failure to NOT ASSESSED",
            cq_lane_row["status"] == "NOT ASSESSED" and "shard-2" in cq_lane_row["reason"], cq_lane_row)

        write_json(os.path.join(td_cb, "verifier-results.json"), [
            {"id": "s-1", "verdict": "confirmed", "reason": "Confirmed at src/auth.py:12."},
        ])
        write_json(os.path.join(td_cb, "eval-results.json"), {
            "evaluations": [
                {"id": "cq-1", "disposition": "post", "eval_reason": "Real resource leak."},
                {"id": [x for x in cb_ids if x != "s-1" and x != "cq-1"][0], "disposition": "post", "eval_reason": "Real nesting risk."},
            ],
        })
        rc_cbf, out_cbf, _ = run_mf(["finalize", td_cb])
        cb_f = json.loads(out_cbf)
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
        run_mf(["merge", td_cb2])
        rc_cb2f, out_cb2f, _ = run_mf(["finalize", td_cb2])
        cb2_f = json.loads(out_cb2f)
        chk("CODEBASE zero-findings sets HEALTHY, event NONE, zero_findings_fast_path=True",
            rc_cb2f == 0 and cb2_f["verdict"] == "HEALTHY" and cb2_f["event"] == "NONE" and cb2_f["zero_findings_fast_path"],
            cb2_f)

        # Malformed JSON & breaker INVALID stderr propagation
        td_err = os.path.join(td, "err-case")
        os.makedirs(td_err)
        with open(os.path.join(td_err, "raw-lanes.json"), "w", encoding="utf-8") as fh:
            fh.write("{corrupt json")
        rc_err, _, err_s = run_mf(["merge", td_err])
        chk("corrupt raw-lanes.json exits 2 cleanly", rc_err == 2 and "merge_findings" in err_s, (rc_err, err_s))

        write_json(os.path.join(td_err, "diff-map.json"), dmap)
        write_json(os.path.join(td_err, "existing-comments.json"), {"inline": [], "reviews": []})
        write_json(os.path.join(td_err, "bundle-summary.json"), {
            "effort": {"resolved": "high"},
            "pr": {"owner": "octo", "repo": "demo", "number": 99, "head_sha": "a" * 40},
            "lane_plan": {"folded": []}, "rules_changed": [],
        })
        write_json(os.path.join(td_err, "raw-lanes.json"), {
            "adherence": {
                "lane": "adherence",
                "findings": [],
                "ac_matrix": [{"ref": "AC9", "text": "Missing AC", "status": "Missing", "evidence": "a.py:10", "test": "untested"}],
            }
        })
        run_mf(["merge", td_err])
        rc_inv, _, err_inv = run_mf(["finalize", td_err])
        chk("finalize exits 1 and prints INVALID: to stderr when breaker fails",
            rc_inv == 1 and "INVALID:" in err_inv, (rc_inv, err_inv))


        # Verification outcomes: plausible and unverified CRITICAL/HIGH keep their level and count at every effort
        for eff in ("high", "low"):
            td_v = os.path.join(td, f"verify-{eff}")
            os.makedirs(td_v)
            write_json(os.path.join(td_v, "diff-map.json"), dmap)
            write_json(os.path.join(td_v, "existing-comments.json"), {"inline": [], "reviews": []})
            write_json(os.path.join(td_v, "bundle-summary.json"), {
                "effort": {"resolved": eff},
                "pr": {"owner": "octo", "repo": "demo", "number": 21, "head_sha": "d" * 40, "author_is_me": False},
                "lane_plan": {"folded": []}, "rules_changed": [],
            })
            lane_name = "code-quality" if eff == "high" else "critical-risk"
            write_json(os.path.join(td_v, "raw-lanes.json"), {
                lane_name: {"lane": lane_name, "findings": [
                    make_f("v-1", lane_name, "HIGH", "a.py:10", "Plausible high", "Result depends on runtime config."),
                    make_f("v-2", lane_name, "HIGH", "a.py:11", "No verifier result", "Nobody checked this one."),
                    make_f("v-3", lane_name, "HIGH", "a.py:12", "Confirmed high", "Verified by a verifier."),
                ]},
            })
            write_json(os.path.join(td_v, "verifier-results.json"), [
                {"id": "v-1", "verdict": "plausible", "reason": "Path exists; config not visible."},
                {"id": "v-3", "verdict": "confirmed", "reason": "a.py:12 confirms it."},
            ])
            run_mf(["merge", td_v, "--effort", eff])
            rc_v, out_v, _ = run_mf(["finalize", td_v, "--effort", eff])
            sv = json.loads(out_v)
            fz = json.load(open(os.path.join(td_v, "findings.json")))
            by_id = {f["id"]: f for f in (fz.get("findings") or [])} if isinstance(fz, dict) else {}
            vmap = {f.get("title"): (f.get("verified"), f.get("severity")) for f in by_id.values()}
            chk(f"{eff}: plausible keeps HIGH, missing verifier result is unverified, confirmed stays confirmed",
                rc_v == 0 and vmap.get("Plausible high") == ("plausible", "HIGH")
                and vmap.get("No verifier result") == ("unverified", "HIGH")
                and vmap.get("Confirmed high") == ("confirmed", "HIGH"), (vmap, sv))
            chk(f"{eff}: plausible and unverified HIGH need an evaluation follow-up, verdict is REQUEST CHANGES, next_step fix",
                {e["title"] for e in sv["eval_followup_needed"]} >= {"Plausible high", "No verifier result"}
                and sv["verdict"] == "REQUEST CHANGES" and sv["next_step"] == "fix", sv)

            # Evaluation cannot drop or hide a plausible/unverified HIGH
            write_json(os.path.join(td_v, "eval-results.json"), {"lane": "evaluation", "evaluations": [
                {"id": "v-1", "disposition": "drop", "reason_code": "low_signal", "eval_reason": "Try to drop."},
                {"id": "v-2", "disposition": "chat-only", "reason_code": "info_only", "eval_reason": "Try to hide."},
            ]})
            rc_v2, out_v2, _ = run_mf(["finalize", td_v, "--effort", eff])
            sv2 = json.loads(out_v2)
            chk(f"{eff}: evaluation drop/chat-only on plausible/unverified HIGH is overridden to post",
                rc_v2 == 0 and sv2["counts"]["post"] == 3 and sv2["counts"]["dropped_by_eval"] == 0
                and sv2["counts"]["chat_only"] == 0 and len(sv2["floor_overrides"]) >= 2, sv2)

    fails = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            fails += 1
    print(f"\n{len(checks) - fails}/{len(checks)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_tests())
