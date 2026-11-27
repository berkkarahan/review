#!/usr/bin/env python3
"""Table-driven tests for scripts/breaker.py. Run: python3 -I -B test_breaker.py
At level=PR, breaker.py validates structure, safety floor, carry continuity, counts, quality-gate
band, blocking_signals, and the post-review verdict decision (APPROVE | COMMENT | REQUEST_CHANGES)
with Soft Guardrail enforcement. At level=CODEBASE, breaker.py computes deterministic codebase
health states (NEEDS ACTION / IMPROVEMENTS RECOMMENDED / HEALTHY). Exit code 0 when all pass."""
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "breaker.py"))
spec = importlib.util.spec_from_file_location("breaker", SCRIPT)
breaker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(breaker)

_n = [0]


def F(sev, lane="code-quality", **kw):
    _n[0] += 1
    f = {"id": f"X{_n[0]}", "lane": lane, "severity": sev, "confidence": "HIGH",
         "title": f"t{_n[0]}", "location": f"a.py:{_n[0]}", "issue": "i", "fix": "f"}
    if sev in ("CRITICAL", "HIGH"):
        f["trigger"] = "input X gives result Y"
        f["verified"] = "confirmed"
    f.update(kw)
    return f


def L(*names, **states):
    out = [{"lane": n} for n in names]
    for n, st in states.items():
        out.append({"lane": n.replace("_", "-"), "status": st, "reason": "r"})
    return out


def AC(ref, status):
    return {"ref": ref, "text": "t", "status": status, "evidence": "—", "test": "untested"}


def DEC(verdict, reason="Holistic review decision.", override_reason=None):
    d = {"verdict": verdict, "reason": reason}
    if override_reason is not None:
        d["override_reason"] = override_reason
    return d


FULL = ["adherence", "code-quality", "security", "performance", "conventions"]
RC, CM, AP = "REQUEST_CHANGES", "COMMENT", "APPROVE"

# (name, effort, input data, options, expected subset)
CASES = []


def add(name, effort, findings=(), ac=(), lanes=None, opts=None, decision=None, **expect):
    data = {"findings": list(findings), "ac_matrix": list(ac),
            "lanes": L(*FULL) if lanes is None else lanes}
    if decision is not None:
        data["decision"] = decision
    CASES.append((name, effort, data, opts or {}, expect))


# --- 1. level=PR pending decision vs explicit verdicts regardless of inline comments ---
for lvl in ("high", "medium"):
    add(f"{lvl}: no decision -> valid, verdict_pending, blocking_signals populated", lvl,
        [F("CRITICAL"), F("HIGH")], valid=True, verdict=None, event=None, next_step=None,
        verdict_pending=True, blocking_signals=["open CRITICAL (1)", "open HIGH (1)"])
    add(f"{lvl}: no decision + require_verdict -> invalid", lvl, [],
        opts={"require_verdict": True}, valid=False, verdict=None, verdict_pending=True)

    # APPROVE regardless of non-blocking inline comments (0, LOW, MEDIUM, QUESTION)
    add(f"{lvl}: APPROVE with 0 findings", lvl, [], decision=DEC(AP),
        valid=True, verdict=AP, event="APPROVE", emoji="🟢", phrase="Ready to merge",
        next_step="done", verdict_pending=False, verdict_overrode_blocking=False)
    add(f"{lvl}: APPROVE with LOW finding", lvl, [F("LOW")], decision=DEC(AP),
        valid=True, verdict=AP, event="APPROVE", next_step="done")
    add(f"{lvl}: APPROVE with MEDIUM inline comment (no override needed)", lvl, [F("MEDIUM")],
        decision=DEC(AP, "One optional cleanup suggestion does not block merge."),
        valid=True, verdict=AP, event="APPROVE", emoji="🟢", phrase="Ready to merge",
        next_step="done", verdict_overrode_blocking=False)
    add(f"{lvl}: APPROVE with QUESTION inline comment (no override needed)", lvl, [F("QUESTION")],
        decision=DEC(AP, "Non-blocking question can be answered after merge."),
        valid=True, verdict=AP, event="APPROVE", next_step="done", verdict_overrode_blocking=False)
    add(f"{lvl}: APPROVE with Partial/Not verifiable ACs only", lvl, [],
        [AC("AC1", "Partial"), AC("AC2", "Not verifiable"), AC("AC3", "Met")],
        decision=DEC(AP), valid=True, verdict=AP, event="APPROVE", unsatisfied=0)
    add(f"{lvl}: APPROVE with pre-existing CRITICAL (excluded at PR)", lvl,
        [F("CRITICAL", introduced_by_pr="pre-existing")], decision=DEC(AP),
        valid=True, verdict=AP, blocking_signals=[])
    add(f"{lvl}: APPROVE with refuted HIGH (excluded)", lvl, [F("HIGH", verified="refuted")],
        decision=DEC(AP), valid=True, verdict=AP, blocking_signals=[])
    add(f"{lvl}: APPROVE with breaking + migration (not blocking)", lvl,
        [F("MEDIUM", breaking={"consumers": "c", "migration": "Call the v2 route."})],
        decision=DEC(AP), valid=True, verdict=AP, blocking_signals=[])

    # COMMENT regardless of inline comments (0, LOW, MEDIUM, HIGH, CRITICAL)
    add(f"{lvl}: COMMENT with 0 findings", lvl, [], decision=DEC(CM, "Leaving architectural notes."),
        valid=True, verdict=CM, event="COMMENT", emoji="🟡", phrase="Comments for the author",
        next_step="document", blocking_signals=[])
    add(f"{lvl}: COMMENT with MEDIUM finding", lvl, [F("MEDIUM")], decision=DEC(CM),
        valid=True, verdict=CM, event="COMMENT", next_step="document")
    add(f"{lvl}: COMMENT with open HIGH (no override_reason needed)", lvl, [F("HIGH")],
        decision=DEC(CM, "Sharing high-risk feedback without blocking."),
        valid=True, verdict=CM, event="COMMENT", next_step="document", verdict_overrode_blocking=False)
    add(f"{lvl}: COMMENT with NOT ASSESSED lane -> next_step rerun", lvl, [],
        lanes=L("adherence", "code-quality", security="NOT ASSESSED"),
        decision=DEC(CM, "Security lane failed so approval is withheld."),
        valid=True, verdict=CM, event="COMMENT", complete=False, next_step="rerun")

    # REQUEST_CHANGES regardless of inline comments (0, LOW, MEDIUM, HIGH, CRITICAL)
    add(f"{lvl}: REQUEST_CHANGES with 0 inline comments", lvl, [],
        decision=DEC(RC, "PR scope diverges from the linked architecture spec."),
        valid=True, verdict=RC, event="REQUEST_CHANGES", emoji="🔴", phrase="Not ready to merge",
        next_step="fix", blocking_signals=[])
    add(f"{lvl}: REQUEST_CHANGES with only LOW finding", lvl, [F("LOW")], decision=DEC(RC),
        valid=True, verdict=RC, event="REQUEST_CHANGES", next_step="fix")
    add(f"{lvl}: REQUEST_CHANGES with CRITICAL, hold_event default", lvl, [F("CRITICAL")],
        decision=DEC(RC), valid=True, verdict=RC, event="REQUEST_CHANGES", next_step="fix")
    add(f"{lvl}: REQUEST_CHANGES with CRITICAL, hold_event COMMENT", lvl, [F("CRITICAL")],
        opts={"hold_event": "COMMENT"}, decision=DEC(RC),
        valid=True, verdict=RC, event="COMMENT", next_step="fix")

    # Soft Guardrail: APPROVE over active blocking_signals requires override_reason
    add(f"{lvl}: APPROVE over CRITICAL without override_reason -> invalid", lvl, [F("CRITICAL")],
        decision=DEC(AP), valid=False, verdict=AP, next_step=None, verdict_overrode_blocking=False)
    add(f"{lvl}: APPROVE over CRITICAL with override_reason -> valid", lvl, [F("CRITICAL")],
        decision=DEC(AP, "Safe behind disabled feature flag.", "Feature flag is off in production."),
        valid=True, verdict=AP, event="APPROVE", next_step="done", verdict_overrode_blocking=True)
    add(f"{lvl}: APPROVE over HIGH without override_reason -> invalid", lvl, [F("HIGH")],
        decision=DEC(AP), valid=False, verdict=AP, next_step=None)
    add(f"{lvl}: APPROVE over HIGH with override_reason -> valid", lvl, [F("HIGH")],
        decision=DEC(AP, "Approved with tracked follow-up.", "Follow-up ticket fixes edge case today."),
        valid=True, verdict=AP, event="APPROVE", next_step="done", verdict_overrode_blocking=True)
    add(f"{lvl}: APPROVE over Missing AC without override_reason -> invalid", lvl,
        [F("HIGH", ac_ref="AC1")], [AC("AC1", "Missing")], decision=DEC(AP),
        valid=False, unsatisfied=1)
    add(f"{lvl}: APPROVE over Contradicted AC without override_reason -> invalid", lvl,
        [F("MEDIUM", ac_ref="B2")], [AC("B2", "Contradicted")], decision=DEC(AP),
        valid=False, unsatisfied=1)
    add(f"{lvl}: APPROVE over unmigrated breaking change without override_reason -> invalid", lvl,
        [F("MEDIUM", breaking={"consumers": "c", "migration": ""})], decision=DEC(AP), valid=False)
    add(f"{lvl}: APPROVE over breaking migration 'none' with override_reason -> valid", lvl,
        [F("MEDIUM", breaking={"consumers": "c", "migration": "none"})],
        decision=DEC(AP, "Approved for internal test branch.", "Only internal test harness calls this."),
        valid=True, verdict=AP, verdict_overrode_blocking=True)
    add(f"{lvl}: APPROVE over NOT ASSESSED lane without override_reason -> invalid", lvl, [],
        lanes=L("adherence", "code-quality", security="NOT ASSESSED"),
        decision=DEC(AP), valid=False, complete=False)
    add(f"{lvl}: APPROVE over NOT ASSESSED lane with override_reason -> valid", lvl, [],
        lanes=L("adherence", "code-quality", security="NOT ASSESSED"),
        decision=DEC(AP, "Approved after manual security check.", "Lead verified diff lines manually."),
        valid=True, verdict=AP, event="APPROVE", complete=False, next_step="done",
        verdict_overrode_blocking=True)

    # author_is_me forces GitHub event to COMMENT and uses self-review phrases while preserving verdict
    add(f"{lvl}: author_is_me REQUEST_CHANGES", lvl, [F("CRITICAL")],
        opts={"author_is_me": True}, decision=DEC(RC),
        valid=True, verdict=RC, event="COMMENT", phrase="Not ready for review")
    add(f"{lvl}: author_is_me COMMENT", lvl, [],
        opts={"author_is_me": True}, decision=DEC(CM),
        valid=True, verdict=CM, event="COMMENT", phrase="Comments for self-review")
    add(f"{lvl}: author_is_me APPROVE", lvl, [],
        opts={"author_is_me": True}, decision=DEC(AP),
        valid=True, verdict=AP, event="COMMENT", phrase="Ready for review")

    # open counts
    add(f"{lvl}: open counts", lvl,
        [F("CRITICAL"), F("HIGH"), F("HIGH"), F("MEDIUM"), F("LOW"), F("QUESTION")],
        decision=DEC(RC),
        open={"critical": 1, "high": 2, "medium": 1, "low": 1, "question": 1})

# --- 2. Team-gate band (informational, independent of verdict) ---
add("band: security HIGH -> FAIL", "high", [F("HIGH", lane="security")], decision=DEC(RC), band="FAIL")
add("band: performance MEDIUM -> CONCERNS", "high", [F("MEDIUM", lane="performance")], decision=DEC(AP), band="CONCERNS")
add("band: code-quality LOW -> PASS", "high", [F("LOW", lane="code-quality")], decision=DEC(AP), band="PASS")
add("band: none -> PASS", "high", [], decision=DEC(AP), band="PASS")
add("band: QUESTION counts as none -> PASS", "high", [F("QUESTION", lane="security")], decision=DEC(AP), band="PASS")
add("band: worst wins", "high", [F("MEDIUM", lane="performance"), F("CRITICAL", lane="security"),
                                 F("LOW", lane="code-quality")], decision=DEC(RC), band="FAIL")
add("band: CRITICAL in adherence lane is not banded", "high", [F("CRITICAL", lane="adherence")],
    decision=DEC(RC), band="PASS")
add("band: legacy business-logic lane alias is still accepted", "high", [F("CRITICAL", lane="business-logic")],
    lanes=L("business-logic", "code-quality", "security", "performance", "conventions"),
    decision=DEC(RC), band="PASS")
add("band: NOT ASSESSED lane caps below PASS, complete false", "high", [],
    lanes=L("adherence", "code-quality", "performance", security="NOT ASSESSED"),
    decision=DEC(CM), band="CONCERNS", complete=False, verdict=CM, next_step="rerun")
add("band: NOT ASSESSED plus HIGH stays FAIL", "high", [F("HIGH", lane="code-quality")],
    lanes=L("adherence", "code-quality", security="NOT ASSESSED"),
    decision=DEC(RC), band="FAIL", complete=False, next_step="fix")
add("band: no band lane ran -> n/a", "high", [], lanes=L("adherence", "conventions"), decision=DEC(AP), band="n/a")
add("band: abstained lane is returned, not failed", "high", [],
    lanes=L("adherence", "code-quality", security="abstained"), decision=DEC(AP), band="PASS", complete=True)
add("band: medium folded -> PASS (partial)", "medium", [], lanes=L("adherence", "code-quality", "conventions"),
    decision=DEC(AP), band="PASS (partial)")
CASES[-1][2]["folded"] = ["security"]
add("band: medium folded + MEDIUM -> CONCERNS (partial)", "medium", [F("MEDIUM", lane="code-quality")],
    lanes=L("adherence", "code-quality", "conventions"), decision=DEC(AP), band="CONCERNS (partial)")
CASES[-1][2]["folded"] = ["performance", "security"]
add("band: medium folded + HIGH -> FAIL (partial)", "medium", [F("HIGH", lane="code-quality")],
    lanes=L("adherence", "code-quality", "conventions"), decision=DEC(RC), band="FAIL (partial)")
CASES[-1][2]["folded"] = ["security"]
add("band: medium nothing folded -> plain PASS", "medium", [], lanes=L("adherence", "code-quality", "security"),
    decision=DEC(AP), band="PASS")

# --- 3. Low and Skim: same verdict & Soft Guardrail rules as high; band is n/a ---
add("low: REQUEST_CHANGES with CRITICAL", "low", [F("CRITICAL")], lanes=L("critical-risk"),
    decision=DEC(RC), valid=True, verdict=RC, event="REQUEST_CHANGES", phrase="Not ready to merge", emoji="🔴", band="n/a")
add("low: APPROVE with MEDIUM + QUESTION", "low", [F("QUESTION"), F("MEDIUM")], lanes=[],
    decision=DEC(AP), valid=True, verdict=AP, event="APPROVE", emoji="🟢", phrase="Ready to merge", band="n/a")
add("low: REQUEST_CHANGES with 0 findings", "low", [], lanes=[],
    decision=DEC(RC), valid=True, verdict=RC, event="REQUEST_CHANGES", next_step="fix")
add("low: author_is_me", "low", [F("HIGH")], lanes=[], opts={"author_is_me": True},
    decision=DEC(RC), valid=True, verdict=RC, event="COMMENT", phrase="Not ready for review")

SK = L("skim")
add("skim: REQUEST_CHANGES with CRITICAL", "skim", [F("CRITICAL")], lanes=SK,
    decision=DEC(RC), valid=True, verdict=RC, event="REQUEST_CHANGES", phrase="Not ready to merge", band="n/a")
add("skim: REQUEST_CHANGES hold COMMENT", "skim", [F("CRITICAL")], lanes=SK,
    opts={"hold_event": "COMMENT"}, decision=DEC(RC), valid=True, verdict=RC, event="COMMENT")
add("skim: APPROVE with MEDIUM + QUESTION", "skim", [F("MEDIUM"), F("QUESTION")], lanes=SK,
    decision=DEC(AP), valid=True, verdict=AP, event="APPROVE", phrase="Ready to merge")
add("skim: COMMENT with 0 findings", "skim", [], lanes=SK,
    decision=DEC(CM), valid=True, verdict=CM, event="COMMENT", phrase="Comments for the author")
add("skim: in_profile and ci have no effect", "skim", [], lanes=SK,
    opts={"in_profile": False, "ci": "failing: build"}, decision=DEC(AP), valid=True, verdict=AP, event="APPROVE")
add("skim: author_is_me APPROVE", "skim", [], lanes=SK,
    opts={"author_is_me": True}, decision=DEC(AP), valid=True, verdict=AP, event="COMMENT", phrase="Ready for review")
add("skim: no AC matrix, rows ignored", "skim", [], [AC("AC1", "Missing")], lanes=SK,
    decision=DEC(AP), valid=True, verdict=AP, unsatisfied=0)
add("skim: lane failed -> invalid even with decision", "skim", [], lanes=L(skim="NOT ASSESSED"),
    decision=DEC(CM), valid=False, next_step=None)

# --- 4. Plausible and unverified CRITICAL/HIGH enter blocking_signals at every effort ---
for _eff, _lanes in (("high", None), ("medium", None), ("low", []), ("skim", SK)):
    add(f"{_eff}: unverified HIGH triggers Soft Guardrail on APPROVE without override", _eff,
        [F("HIGH", verified="unverified")], lanes=_lanes, decision=DEC(AP),
        valid=False, blocking_signals=["open HIGH (1)"])
    add(f"{_eff}: plausible HIGH triggers Soft Guardrail on APPROVE with override -> valid", _eff,
        [F("HIGH", verified="plausible")], lanes=_lanes,
        decision=DEC(AP, "Safe in current config.", "Runtime config prevents the path."),
        valid=True, verdict=AP, verdict_overrode_blocking=True)
    add(f"{_eff}: unverified CRITICAL with REQUEST_CHANGES -> valid", _eff,
        [F("CRITICAL", verified="unverified")], lanes=_lanes, decision=DEC(RC),
        valid=True, verdict=RC, next_step="fix")

# --- 5. Additive carry/eval fields ---
add("new: no status counts as todo, no origin as new", "high", [F("HIGH")],
    decision=DEC(RC), open={"high": 1}, declined_blocking=0)
add("new: fixed finding is not open or blocking", "high", [F("CRITICAL", status="fixed", commit="abc1234")],
    decision=DEC(AP), valid=True, verdict=AP, open={"critical": 0}, blocking_signals=[])
add("new: origin existing is not open or blocking", "high", [F("CRITICAL", origin="existing")],
    decision=DEC(AP), valid=True, verdict=AP, open={"critical": 0}, blocking_signals=[])
add("new: declined new HIGH triggers Soft Guardrail on APPROVE without override", "high",
    [F("HIGH", status="declined", reason="false positive")],
    decision=DEC(AP), valid=False, declined_blocking=1, open={"high": 0})
add("new: declined new HIGH APPROVE with override -> valid", "high",
    [F("HIGH", status="declined", reason="false positive")],
    decision=DEC(AP, "Owner accepted risk.", "Decline reason verified by owner."),
    valid=True, verdict=AP, declined_blocking=1, verdict_overrode_blocking=True)
add("new: declined new CRITICAL banded FAIL", "high",
    [F("CRITICAL", lane="security", status="declined", reason="r")],
    decision=DEC(CM), valid=True, band="FAIL", declined_blocking=1)
add("new: declined MEDIUM is not blocking", "high", [F("MEDIUM", status="declined", reason="r")],
    decision=DEC(AP), valid=True, verdict=AP, declined_blocking=0, blocking_signals=[])
add("new: declined existing HIGH is not blocking", "high",
    [F("HIGH", status="declined", reason="r", origin="existing")],
    decision=DEC(AP), valid=True, verdict=AP, declined_blocking=0, blocking_signals=[])
add("eval: evaluation lane returned -> valid", "high", [F("MEDIUM", eval_disposition="post")],
    lanes=L(*FULL, "evaluation"), decision=DEC(AP), valid=True, verdict=AP)
add("eval: evaluation lane abstained -> complete", "high", [],
    lanes=L(*FULL, evaluation="abstained"), decision=DEC(AP), valid=True, complete=True, verdict=AP)
add("eval: evaluation lane NOT ASSESSED -> incomplete", "high", [],
    lanes=L(*FULL, evaluation="NOT ASSESSED"), decision=DEC(CM), valid=True, complete=False, verdict=CM, next_step="rerun")
add("eval: chat-only and drop excluded from open counts and blocking_signals", "high",
    [F("MEDIUM", eval_disposition="chat-only"), F("LOW", eval_disposition="drop")],
    lanes=L(*FULL, "evaluation"), decision=DEC(AP), valid=True, verdict=AP, event="APPROVE",
    open={"critical": 0, "high": 0, "medium": 0, "low": 0, "question": 0}, blocking_signals=[])

# --- 6. Invalid inputs & invalid verdict decisions ---
add("invalid: legacy verdict 'REQUEST CHANGES' rejected", "high", [],
    decision={"verdict": "REQUEST CHANGES", "reason": "old"}, valid=False, verdict=None)
add("invalid: legacy verdict 'APPROVE WITH COMMENTS' rejected", "high", [],
    decision={"verdict": "APPROVE WITH COMMENTS", "reason": "old"}, valid=False, verdict=None)
add("invalid: bogus verdict rejected", "high", [],
    decision={"verdict": "LGTM", "reason": "nope"}, valid=False, verdict=None)
add("invalid: empty reason rejected", "high", [],
    decision={"verdict": "APPROVE", "reason": "   "}, valid=False, verdict=AP)
add("invalid: non-string override_reason rejected", "high", [],
    decision={"verdict": "APPROVE", "reason": "ok", "override_reason": 123}, valid=False)
add("invalid: Missing AC with no naming finding", "high", [], [AC("AC1", "Missing")],
    decision=DEC(RC), valid=False, verdict=RC, next_step=None)
add("invalid: Contradicted AC named by another ref", "high", [F("HIGH", ac_ref="AC2")],
    [AC("AC1", "Contradicted")], decision=DEC(RC), valid=False)
add("valid: ac_ref lists several refs", "high", [F("HIGH", ac_ref="AC1, B2")],
    [AC("B2", "Missing")], decision=DEC(RC), valid=True)
add("invalid: HIGH with no trigger", "high", [F("HIGH", trigger="")], decision=DEC(RC), valid=False)
add("invalid: HIGH with n/a trigger", "high", [F("HIGH", trigger="n/a")], decision=DEC(RC), valid=False)
add("valid: conventions HIGH with n/a (convention)", "high",
    [F("HIGH", lane="conventions", trigger="n/a (convention)")], decision=DEC(RC), valid=True)
add("valid: unverified HIGH is kept at its level", "high", [F("HIGH", verified="unverified")],
    decision=DEC(RC), valid=True)
add("invalid: bad severity", "high", [F("BLOCKER")], decision=DEC(RC), valid=False)
add("invalid: bad status", "high", [F("LOW", status="open")], decision=DEC(AP), valid=False)
add("invalid: bad origin", "high", [F("LOW", origin="old")], decision=DEC(AP), valid=False)
add("invalid: fixed without commit", "high", [F("LOW", status="fixed")], decision=DEC(AP), valid=False)
add("invalid: declined without reason", "high", [F("LOW", status="declined")], decision=DEC(AP), valid=False)
add("invalid: duplicate ids", "high", [F("LOW", id="D1"), F("LOW", id="D1")], decision=DEC(AP), valid=False)
add("invalid: lane NOT ASSESSED needs a reason", "high", [],
    lanes=[{"lane": "security", "status": "NOT ASSESSED"}], decision=DEC(CM), valid=False)
add("invalid: unknown lane status", "high", [], lanes=[{"lane": "security", "status": "done"}],
    decision=DEC(AP), valid=False)
add("invalid: lanes missing at high", "high", [], lanes=None, decision=DEC(AP), valid=True)
CASES[-1][2].pop("lanes")
CASES[-1][4]["valid"] = False
add("invalid: challenges unknown id", "high", [F("LOW", challenges="Z9")], decision=DEC(AP), valid=False)
add("valid: challenges a declined id", "high",
    [F("MEDIUM", id="Z9", status="declined", reason="r"), F("HIGH", challenges="Z9")],
    decision=DEC(RC), valid=True)
add("invalid: cleared without evidence", "high", [],
    lanes=[{"lane": "security", "cleared": [{"id": "B1"}]}], decision=DEC(AP), valid=False)
add("eval: invalid eval_disposition rejected", "high", [F("MEDIUM", eval_disposition="maybe")],
    decision=DEC(AP), valid=False)
add("eval: eval_downgraded without eval_reason rejected", "high", [F("MEDIUM", eval_downgraded=True)],
    decision=DEC(AP), valid=False)


def run_case(name, effort, data, opts, expect):
    o = dict(opts)
    o["effort"] = effort
    before = copy.deepcopy(data)
    out = breaker.compute(data, o)
    problems = []
    if data != before:
        problems.append("input was mutated")
    for k, want in expect.items():
        got = out.get(k)
        if isinstance(want, dict):
            got = {kk: got.get(kk) for kk in want}
        if got != want:
            problems.append(f"{k}: want {want!r}, got {got!r}")
    return problems, out


def pre_prev_cases():
    res = []

    def chk(name, ok, detail=""):
        res.append((name, [] if ok else [detail or "failed"]))

    base_lanes = L(*FULL)
    # safety floor (--pre)
    pre = [F("CRITICAL", id="bl-1", title="Wrong total", location="a.py:10"),
           F("HIGH", id="sec-1", title="Missing check", location="b.py:5")]
    post_ok = [F("CRITICAL", id="C1", title="Wrong total", location="a.py:10"),
               F("HIGH", id="H1", title="Other title", location="b.py:6", merged_titles=["Missing check"])]
    o = breaker.compute({"findings": post_ok, "lanes": base_lanes, "decision": DEC(RC)}, {}, pre=pre)
    chk("floor: kept by title and by merged_titles/location", o["valid"], str(o["invalid"]))
    post_drop = [post_ok[0]]
    o = breaker.compute({"findings": post_drop, "lanes": base_lanes, "decision": DEC(RC)}, {}, pre=pre)
    chk("floor: dropped HIGH is invalid", not o["valid"] and "safety floor" in o["invalid"][0], str(o["invalid"]))
    post_low = [post_ok[0], F("MEDIUM", id="M1", title="Missing check", location="b.py:5")]
    o = breaker.compute({"findings": post_low, "lanes": base_lanes, "decision": DEC(RC)}, {}, pre=pre)
    chk("floor: lowered HIGH is invalid", not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))
    pre_unv = [F("HIGH", id="x", title="Not confirmed", verified="unverified")]
    o = breaker.compute({"findings": [], "lanes": base_lanes, "decision": DEC(AP)}, {}, pre=pre_unv)
    chk("floor: unverified pre finding is not on the floor", o["valid"], str(o["invalid"]))

    # effort never changes verdict, event, next_step, or phrase
    inv_pr_sets = {
        "APPROVE with MEDIUM": ([F("MEDIUM")], DEC(AP)),
        "COMMENT with 0 findings": ([], DEC(CM)),
        "REQUEST_CHANGES with 0 findings": ([], DEC(RC)),
        "APPROVE over HIGH with override": ([F("HIGH")], DEC(AP, "Safe.", "Guarded externally.")),
        "APPROVE over HIGH without override": ([F("HIGH")], DEC(AP)),
    }
    for label, (fs, dec) in inv_pr_sets.items():
        seen = {}
        for eff in ("high", "medium", "low", "skim"):
            lanes = {"high": base_lanes, "medium": base_lanes, "low": [], "skim": L("skim")}[eff]
            oo = breaker.compute({"findings": copy.deepcopy(fs), "lanes": lanes, "decision": copy.deepcopy(dec)},
                                 {"effort": eff, "level": "PR"})
            seen[eff] = (oo["valid"], oo["verdict"], oo["event"], oo["next_step"], oo["phrase"])
        chk(f"effort invariance PR: {label}", len(set(seen.values())) == 1, str(seen))

    inv_cb_sets = {
        "HIGH": [F("HIGH")], "MEDIUM": [F("MEDIUM")], "QUESTION": [F("QUESTION")], "none": [],
        "CRITICAL": [F("CRITICAL")], "unverified HIGH": [F("HIGH", verified="unverified")],
    }
    for label, fs in inv_cb_sets.items():
        seen = {}
        for eff in ("high", "medium", "low"):
            lanes = {"high": base_lanes, "medium": base_lanes, "low": []}[eff]
            oo = breaker.compute({"findings": copy.deepcopy(fs), "lanes": lanes}, {"effort": eff, "level": "CODEBASE"})
            seen[eff] = (oo["valid"], oo["verdict"], oo["event"], oo["next_step"], oo["phrase"])
        chk(f"effort invariance CODEBASE: {label}", len(set(seen.values())) == 1, str(seen))

    o = breaker.compute({"findings": [F("HIGH", id="H1", title="Other", location="zz.py:1",
                                         introduced_by_pr="pre-existing")], "lanes": base_lanes, "decision": DEC(AP)}, {},
                        pre=[F("HIGH", id="q", title="Other", location="zz.py:1")])
    chk("floor: moved to pre-existing is kept, not dropped", o["valid"], str(o["invalid"]))

    # evaluation lane safety floor vs non-floor downgrade
    cq_pre = [F("HIGH", id="cq-1", lane="code-quality", title="Verbose helper", location="c.py:10")]
    cq_down_ok = [F("MEDIUM", id="D1", lane="code-quality", title="Verbose helper", location="c.py:10",
                    eval_disposition="post", eval_downgraded=True,
                    eval_reason="Readability issue does not crash on main path.")]
    o = breaker.compute({"findings": cq_down_ok, "lanes": base_lanes + [{"lane": "evaluation"}], "decision": DEC(AP)}, {}, pre=cq_pre)
    chk("eval floor: non-floor HIGH downgraded with eval_reason is valid", o["valid"], str(o["invalid"]))

    crit_pre = [F("CRITICAL", id="sec-c", lane="security", title="SQL injection", location="d.py:4")]
    crit_down = [F("MEDIUM", id="S1", lane="security", title="SQL injection", location="d.py:4",
                   eval_disposition="post", eval_downgraded=True, eval_reason="try lower")]
    o = breaker.compute({"findings": crit_down, "lanes": base_lanes + [{"lane": "evaluation"}], "decision": DEC(CM)}, {}, pre=crit_pre)
    chk("eval floor: confirmed CRITICAL cannot be downgraded", not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))

    sec_pre = [F("HIGH", id="sec-h", lane="security", title="Missing auth", location="d.py:8")]
    sec_down = [F("MEDIUM", id="S2", lane="security", title="Missing auth", location="d.py:8",
                  eval_disposition="post", eval_downgraded=True, eval_reason="try lower")]
    o = breaker.compute({"findings": sec_down, "lanes": base_lanes + [{"lane": "evaluation"}], "decision": DEC(CM)}, {}, pre=sec_pre)
    chk("eval floor: confirmed security HIGH cannot be downgraded", not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))

    ac_pre = [F("HIGH", id="b-h", lane="adherence", title="Missing requirement", location="e.py:3", ac_ref="AC1")]
    ac_down = [F("MEDIUM", id="B1", lane="adherence", title="Missing requirement", location="e.py:3",
                 eval_disposition="post", eval_downgraded=True, eval_reason="try lower")]
    o = breaker.compute({"findings": ac_down, "lanes": base_lanes + [{"lane": "evaluation"}], "decision": DEC(CM)}, {}, pre=ac_pre)
    chk("eval floor: confirmed ac_ref HIGH cannot be downgraded", not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))

    brk_pre = [F("HIGH", id="a-h", lane="compatibility", title="Removed field", location="f.py:2",
                 breaking={"consumers": "mobile", "migration": "none"})]
    brk_down = [F("MEDIUM", id="A1", lane="compatibility", title="Removed field", location="f.py:2",
                  eval_disposition="post", eval_downgraded=True, eval_reason="try lower")]
    o = breaker.compute({"findings": brk_down, "lanes": base_lanes + [{"lane": "evaluation"}], "decision": DEC(CM)}, {}, pre=brk_pre)
    chk("eval floor: confirmed unmigrated breaking HIGH cannot be downgraded",
        not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))

    for disp in ("drop", "chat-only"):
        cq_hidden = [F("HIGH", id="D2", lane="code-quality", title="Verbose helper", location="c.py:10",
                       eval_disposition=disp, eval_reason="hide")]
        o = breaker.compute({"findings": cq_hidden, "lanes": base_lanes + [{"lane": "evaluation"}], "decision": DEC(CM)}, {}, pre=cq_pre)
        chk(f"eval floor: confirmed HIGH cannot be {disp}",
            not o["valid"] and "missing" in " ".join(o["invalid"]), str(o["invalid"]))

        crit_hidden = [F("CRITICAL", id="S9", lane="security", title="SQL injection", location="d.py:4",
                         eval_disposition=disp, eval_reason="hide")]
        o = breaker.compute({"findings": crit_hidden, "lanes": base_lanes + [{"lane": "evaluation"}], "decision": DEC(CM)}, {}, pre=crit_pre)
        chk(f"eval floor: confirmed CRITICAL cannot be {disp}",
            not o["valid"] and "missing" in " ".join(o["invalid"]), str(o["invalid"]))

    # previous carry
    prev = [F("HIGH", id="S2", status="todo", origin="new"),
            F("CRITICAL", id="B1", status="todo", origin="new"),
            F("MEDIUM", id="B3", status="todo", origin="new")]
    cur = [F("HIGH", id="S2", status="todo"), F("CRITICAL", id="B1", status="fixed", commit="abc1234")]
    o = breaker.compute({"findings": cur, "lanes": base_lanes, "decision": DEC(RC)}, {}, previous=prev)
    chk("previous: carried and fixed are accepted", o["valid"], str(o["invalid"]))
    o = breaker.compute({"findings": [cur[0]], "lanes": base_lanes, "decision": DEC(RC)}, {}, previous=prev)
    chk("previous: dropped CRITICAL is invalid", not o["valid"] and "B1" in o["invalid"][0], str(o["invalid"]))
    lanes_cleared = base_lanes + [{"lane": "robustness", "cleared": [{"id": "B1", "evidence": "a.py:3 guards it"}]}]
    o = breaker.compute({"findings": [cur[0]], "lanes": lanes_cleared, "decision": DEC(RC)}, {}, previous=prev)
    chk("previous: cleared with evidence is accepted", o["valid"], str(o["invalid"]))
    cur_low = [F("MEDIUM", id="S2", status="todo"), cur[1]]
    o = breaker.compute({"findings": cur_low, "lanes": base_lanes, "decision": DEC(CM)}, {}, previous=prev)
    chk("previous: lowered HIGH is invalid", not o["valid"] and "S2" in o["invalid"][0], str(o["invalid"]))
    legacy_prev = [F("HIGH", id="H1")]
    o = breaker.compute({"findings": [], "lanes": base_lanes, "decision": DEC(AP)}, {}, previous=legacy_prev)
    chk("previous: legacy file (no status) is skipped with a note", o["valid"] and o["notes"], str(o))

    # Carried fixed/declined MEDIUM/LOW at skim & legacy/existing without trigger
    skim_carried = [
        F("MEDIUM", id="P1", status="declined", reason="Accepted."),
        F("LOW", id="D1", status="fixed", commit="abc1234"),
    ]
    o = breaker.compute({"findings": skim_carried, "lanes": L("skim"), "decision": DEC(AP)}, {"effort": "skim"})
    chk("skim: carried fixed/declined MEDIUM/LOW is valid", o["valid"] and o["verdict"] == AP, str(o))

    leg_open = [F("HIGH", id="X1", trigger="", legacy=True), F("HIGH", id="X2", trigger="", origin="existing")]
    o = breaker.compute({"findings": leg_open, "lanes": base_lanes, "decision": DEC(RC)}, {"effort": "high"})
    chk("legacy/existing HIGH without trigger is valid", o["valid"], str(o["invalid"]))

    # FLOOR_KEYWORDS & title/issue scanning
    for kw_field, kw_val in (
        ("dimension", "secret leak"),
        ("impact", "Exposes user secrets"),
        ("title", "Hardcoded credential in config"),
        ("issue", "Causes silent data-loss on retry"),
        ("issue", "Causes data-corruption in table"),
    ):
        kw_pre = [F("HIGH", id="cq-k", lane="code-quality", **{kw_field: kw_val})]
        kw_down = [F("MEDIUM", id="D1", lane="code-quality", eval_disposition="post",
                     eval_downgraded=True, eval_reason="try lower", **{kw_field: kw_val})]
        o = breaker.compute({"findings": kw_down, "lanes": base_lanes + [{"lane": "evaluation"}], "decision": DEC(CM)}, {}, pre=kw_pre)
        chk(f"eval floor: keyword in {kw_field}={kw_val!r} protects HIGH from downgrade",
            not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o))

    # same_flaw 5-line window, HUNK_LOC, space path, and standing declined_still_present
    pre_5 = [F("HIGH", id="sec-1", location="my dir/app.py @@ -8,2 +10,3 @@", title="Pre title")]
    post_5 = [F("HIGH", id="S1", location="my dir/app.py:15", title="Different title")]
    o = breaker.compute({"findings": post_5, "lanes": base_lanes, "decision": DEC(RC)}, {}, pre=pre_5)
    chk("floor: 5-line window + HUNK_LOC + space path matches", o["valid"], str(o["invalid"]))

    post_dsp = [F("MEDIUM", id="P1", lane="performance", location="a.py:90", title="Old title",
                  status="declined", reason="Small table", declined_still_present=True,
                  merged_titles=["Confirmed repeat"], repeat_locations=["a.py:99"])]
    pre_dsp = [F("HIGH", id="perf-1", lane="performance", location="a.py:99", title="Confirmed repeat")]
    o = breaker.compute({"findings": post_dsp, "lanes": base_lanes, "decision": DEC(CM)}, {}, pre=pre_dsp)
    chk("floor: standing declined_still_present MEDIUM satisfies confirmed HIGH pre and enters blocking_signals",
        o["valid"] and o["declined_blocking"] == 1 and o["verdict"] == CM
        and any("declined_blocking" in s for s in o["blocking_signals"]), str(o))

    # check_previous rejects dropped/chat-only/refuted match without cleared
    for bad_cur in (
        [F("CRITICAL", id="B1", status="todo", eval_disposition="drop")],
        [F("CRITICAL", id="B1", status="todo", eval_disposition="chat-only")],
        [F("CRITICAL", id="B1", status="todo", verified="refuted")],
    ):
        o = breaker.compute({"findings": [cur[0]] + bad_cur, "lanes": base_lanes, "decision": DEC(RC)}, {}, previous=prev)
        chk("previous: excluded match without cleared is invalid", not o["valid"] and "B1" in " ".join(o["invalid"]), str(o))

    # band inspects merged sources
    o = breaker.compute({"findings": [F("HIGH", lane="adherence", sources=["adherence", "security"])],
                         "lanes": base_lanes, "decision": DEC(RC)}, {"effort": "high"})
    chk("band: merged security source in adherence finding gives FAIL", o["band"] == "FAIL", str(o))

    # CODEBASE matrix: unchanged deterministic health states
    o = breaker.compute({"findings": [F("LOW", introduced_by_pr="codebase")],
                         "lanes": L("adherence", "code-quality", "conventions"), "folded": ["security"]},
                        {"level": "CODEBASE", "effort": "medium"})
    chk("CODEBASE medium folded -> HEALTHY, PASS (partial), NONE",
        o["valid"] and o["verdict"] == "HEALTHY" and o["band"] == "PASS (partial)" and o["event"] == "NONE"
        and not o["verdict_pending"], str(o))

    o = breaker.compute({"findings": [F("HIGH", introduced_by_pr="codebase")], "lanes": L("critical-risk")},
                        {"level": "CODEBASE", "effort": "low"})
    chk("CODEBASE low HIGH -> NEEDS ACTION, NONE",
        o["valid"] and o["verdict"] == "NEEDS ACTION" and o["event"] == "NONE", str(o))

    o = breaker.compute({"findings": [F("HIGH", status="declined", reason="wontfix", introduced_by_pr="codebase")],
                         "lanes": base_lanes}, {"level": "CODEBASE", "effort": "high", "author_is_me": True})
    chk("CODEBASE author_is_me + declined_blocking -> IMPROVEMENTS RECOMMENDED, NONE",
        o["valid"] and o["verdict"] == "IMPROVEMENTS RECOMMENDED" and o["event"] == "NONE" and o["declined_blocking"] == 1,
        str(o))

    o = breaker.compute({"findings": [], "lanes": L("skim")}, {"level": "CODEBASE", "effort": "skim"})
    chk("CODEBASE + skim -> invalid", not o["valid"], str(o))

    # Malformed container/unhashable inputs return valid=False cleanly without TypeError
    malformed_cases = [
        ("findings scalar", {"findings": 123, "lanes": []}),
        ("ac_matrix scalar", {"findings": [], "ac_matrix": "bad", "lanes": []}),
        ("lanes scalar", {"findings": [], "lanes": 123}),
        ("folded scalar", {"findings": [], "lanes": L(*FULL), "folded": "none"}),
        ("decision scalar", {"findings": [], "lanes": L(*FULL), "decision": "APPROVE"}),
        ("author_is_me str", {"findings": [], "lanes": L(*FULL), "author_is_me": "false"}),
        ("in_profile str", {"findings": [], "lanes": L("skim"), "in_profile": "false"}),
        ("ci non-str", {"findings": [], "lanes": L("skim"), "ci": 123}),
        ("id list", {"findings": [{"id": ["X1"], "severity": "LOW"}], "lanes": []}),
        ("status list", {"findings": [{"id": "X1", "severity": "LOW", "status": ["todo"]}], "lanes": []}),
        ("origin dict", {"findings": [{"id": "X1", "severity": "LOW", "origin": {}}], "lanes": []}),
        ("eval_disposition list", {"findings": [{"id": "X1", "severity": "LOW", "eval_disposition": ["post"]}], "lanes": []}),
        ("challenges list", {"findings": [{"id": "X1", "severity": "LOW", "challenges": ["S1"]}], "lanes": []}),
        ("locations int", {"findings": [{"id": "X1", "severity": "LOW", "locations": 123}], "lanes": []}),
        ("merged_titles int", {"findings": [{"id": "X1", "severity": "LOW", "merged_titles": 123}], "lanes": []}),
        ("ac status list", {"findings": [], "ac_matrix": [{"ref": "AC1", "status": ["Met"]}], "lanes": []}),
        ("lane name list", {"findings": [], "lanes": [{"lane": ["security"]}]}),
    ]
    for mname, mdata in malformed_cases:
        try:
            mo = breaker.compute(mdata, {"effort": "skim" if "skim" in mname else "high"})
            chk(f"malformed: {mname} -> valid=False", not mo["valid"], str(mo))
        except Exception as exc:
            chk(f"malformed: {mname} -> valid=False", False, f"crashed with {type(exc).__name__}: {exc}")

    try:
        mo = breaker.compute({"findings": [], "lanes": L(*FULL)}, {"effort": "high"}, pre=123, previous=123)
        chk("malformed: pre/previous scalar -> valid=False", not mo["valid"] and len(mo["invalid"]) >= 2, str(mo))
    except Exception as exc:
        chk("malformed: pre/previous scalar -> valid=False", False, f"crashed with {type(exc).__name__}: {exc}")
    return res


def cli_cases():
    res = []
    with tempfile.TemporaryDirectory() as td:
        def write(name, obj):
            p = os.path.join(td, name)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(obj, fh)
            return p

        def run(*args):
            return subprocess.run([sys.executable, "-I", "-B", SCRIPT, *args],
                                  capture_output=True, text=True, encoding="utf-8")

        good = write("good.json", {"findings": [F("MEDIUM")], "lanes": L(*FULL), "decision": DEC(AP, "Looks good.")})
        r = run(good)
        j = json.loads(r.stdout)
        res.append(("cli: valid PR with embedded APPROVE + MEDIUM exits 0 with contract keys",
                    [] if r.returncode == 0 and all(k in j for k in
                     ("open", "unsatisfied", "complete", "band", "blocking_signals", "verdict",
                      "verdict_pending", "verdict_reason", "event", "next_step", "invalid"))
                     and j["verdict"] == AP and j["event"] == "APPROVE" and r.stderr == ""
                    else [f"rc={r.returncode} out={j} stderr={r.stderr!r}"]))

        pending = write("pending.json", {"findings": [F("HIGH")], "lanes": L(*FULL)})
        r = run(pending)
        j = json.loads(r.stdout)
        res.append(("cli: PR without decision exits 0 with verdict_pending=True",
                    [] if r.returncode == 0 and j["verdict_pending"] and j["verdict"] is None
                    and j["blocking_signals"] == ["open HIGH (1)"] else [f"rc={r.returncode} out={j}"]))

        r = run(pending, "--require-verdict")
        res.append(("cli: --require-verdict without decision exits 1 with INVALID",
                    [] if r.returncode == 1 and "INVALID:" in r.stderr else [f"rc={r.returncode} stderr={r.stderr!r}"]))

        r = run(pending, "--verdict", "APPROVE", "--verdict-reason", "Ship it.")
        res.append(("cli: Soft Guardrail rejects APPROVE over open HIGH without override-reason",
                    [] if r.returncode == 1 and "override_reason" in r.stderr else [f"rc={r.returncode} stderr={r.stderr!r}"]))

        r = run(pending, "--verdict", "APPROVE", "--verdict-reason", "Ship it.",
                "--verdict-override-reason", "Behind disabled feature flag.")
        j = json.loads(r.stdout)
        res.append(("cli: Soft Guardrail accepts APPROVE over open HIGH with override-reason",
                    [] if r.returncode == 0 and j["verdict"] == AP and j["verdict_overrode_blocking"]
                    else [f"rc={r.returncode} out={j} stderr={r.stderr!r}"]))

        vfile = write("verdict.json", {"verdict": "REQUEST_CHANGES", "reason": "Architecture mismatch."})
        clean = write("clean.json", {"findings": [], "lanes": L(*FULL)})
        r = run(clean, "--verdict-file", vfile)
        j = json.loads(r.stdout)
        res.append(("cli: --verdict-file REQUEST_CHANGES on 0 findings exits 0",
                    [] if r.returncode == 0 and j["verdict"] == RC and j["event"] == "REQUEST_CHANGES"
                    else [f"rc={r.returncode} out={j} stderr={r.stderr!r}"]))

        bad = write("bad.json", {"findings": [], "ac_matrix": [AC("AC1", "Missing")], "lanes": L(*FULL),
                                 "decision": DEC(RC)})
        r = run(bad)
        res.append(("cli: invalid input exits 1 with an INVALID line on stderr",
                    [] if r.returncode == 1 and r.stderr.startswith("INVALID: ") and json.loads(r.stdout)["verdict"] == RC
                    else [f"rc={r.returncode} stderr={r.stderr!r}"]))

        h1 = hashlib.sha256(open(good, "rb").read()).hexdigest()
        run(good, "--effort", "medium")
        h2 = hashlib.sha256(open(good, "rb").read()).hexdigest()
        res.append(("cli: input file is not changed", [] if h1 == h2 else ["file changed"]))

        r = run(os.path.join(td, "missing.json"))
        res.append(("cli: unreadable file exits 1 with INVALID", [] if r.returncode == 1 and "INVALID" in r.stderr
                    else [f"rc={r.returncode}"]))
        r = run(good, "--effort", "bogus")
        res.append(("cli: bad flag value exits 2", [] if r.returncode == 2 else [f"rc={r.returncode}"]))
        r = run(good, "--verdict", "BOGUS")
        res.append(("cli: bad --verdict flag exits 2", [] if r.returncode == 2 else [f"rc={r.returncode}"]))

        skim = write("skim.json", {"findings": [], "lanes": L("skim"), "decision": DEC(AP)})
        r = run(skim, "--effort", "skim", "--in-profile", "--ci", "passing")
        res.append(("cli: skim flags with APPROVE decision give APPROVE", [] if json.loads(r.stdout)["event"] == "APPROVE"
                    else [r.stdout]))
        r = run(skim, "--effort", "skim", "--in-profile", "--ci", "passing", "--author-is-me")
        res.append(("cli: --author-is-me gives COMMENT", [] if json.loads(r.stdout)["event"] == "COMMENT"
                    else [r.stdout]))
        bare = write("bare.json", [F("LOW")])
        r = run(bare, "--effort", "low", "--verdict", "APPROVE", "--verdict-reason", "Clean low review.")
        res.append(("cli: bare array input works at low", [] if r.returncode == 0 and json.loads(r.stdout)["verdict"] == AP
                    else [f"rc={r.returncode} {r.stderr}"]))

        # CODEBASE level cases (unchanged)
        cb_crit = write("cb_crit.json", {"findings": [F("CRITICAL", introduced_by_pr="pre-existing")], "lanes": L(*FULL)})
        r = run(cb_crit, "--level", "CODEBASE")
        j = json.loads(r.stdout)
        res.append(("cli CODEBASE: pre-existing CRITICAL counts -> NEEDS ACTION, event NONE",
                    [] if r.returncode == 0 and j["level"] == "CODEBASE" and j["verdict"] == "NEEDS ACTION"
                    and j["phrase"] == "Codebase needs urgent fixes" and j["event"] == "NONE" and j["open"]["critical"] == 1
                    else [f"rc={r.returncode} out={j}"]))

        cb_med = write("cb_med.json", {"findings": [F("MEDIUM", introduced_by_pr="codebase")], "lanes": L(*FULL)})
        r = run(cb_med, "--level", "CODEBASE")
        j = json.loads(r.stdout)
        res.append(("cli CODEBASE: MEDIUM -> IMPROVEMENTS RECOMMENDED, event NONE",
                    [] if r.returncode == 0 and j["verdict"] == "IMPROVEMENTS RECOMMENDED"
                    and j["phrase"] == "Codebase needs targeted improvements" and j["event"] == "NONE"
                    else [f"rc={r.returncode} out={j}"]))

        cb_ok = write("cb_ok.json", {"findings": [F("LOW", introduced_by_pr="codebase")], "lanes": L(*FULL)})
        r = run(cb_ok, "--level", "CODEBASE")
        j = json.loads(r.stdout)
        res.append(("cli CODEBASE: only LOW -> HEALTHY, event NONE",
                    [] if r.returncode == 0 and j["verdict"] == "HEALTHY"
                    and j["phrase"] == "Codebase is in good shape" and j["event"] == "NONE"
                    else [f"rc={r.returncode} out={j}"]))

        cb_inc = write("cb_inc.json", {"findings": [], "lanes": L("adherence", "code-quality", security="NOT ASSESSED")})
        r = run(cb_inc, "--level", "CODEBASE")
        j = json.loads(r.stdout)
        res.append(("cli CODEBASE: NOT ASSESSED caps HEALTHY to IMPROVEMENTS RECOMMENDED",
                    [] if r.returncode == 0 and j["verdict"] == "IMPROVEMENTS RECOMMENDED" and not j["complete"]
                    else [f"rc={r.returncode} out={j}"]))

        r = run(skim, "--level", "CODEBASE", "--effort", "skim")
        res.append(("cli CODEBASE: --effort skim exits 1 with INVALID",
                    [] if r.returncode == 1 and "INVALID:" in r.stderr else [f"rc={r.returncode} stderr={r.stderr!r}"]))

        embedded = write("embedded.json", {
            "level": "CODEBASE", "effort": "medium", "author_is_me": True,
            "findings": [F("LOW", introduced_by_pr="codebase")], "lanes": L("adherence", "code-quality"),
        })
        r = run(embedded)
        j = json.loads(r.stdout)
        res.append(("cli: embedded breaker-input options work and CLI overrides",
                    [] if r.returncode == 0 and j["level"] == "CODEBASE" and j["effort"] == "medium" and j["event"] == "NONE"
                    else [f"rc={r.returncode} out={j}"]))

        r = run("--help")
        res.append(("cli: --help exits 0", [] if r.returncode == 0 and "breaker.py" in r.stdout else [f"rc={r.returncode}"]))
    return res


def main():
    fails = 0
    total = 0
    for case in CASES:
        problems, out = run_case(*case)
        total += 1
        tag = "PASS" if not problems else "FAIL"
        fails += bool(problems)
        print(f"{tag}  {case[0]}" + ("" if not problems else "  <- " + "; ".join(problems)))
    for name, problems in pre_prev_cases() + cli_cases():
        total += 1
        fails += bool(problems)
        print(f"{'PASS' if not problems else 'FAIL'}  {name}" + ("" if not problems else "  <- " + "; ".join(problems)))
    print(f"\n{total - fails}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
