#!/usr/bin/env python3
"""Table-driven tests for scripts/breaker.py. Run: python3 -I -B test_breaker.py
Each row is a verdict rule of output/verdict.md §Verdict, effort/low.md §Low verdict phrases or
§Skim verdict. Input rows that use none of the new optional fields prove that the verdicts
did not change. Exit code 0 when all pass."""
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


FULL = ["adherence", "code-quality", "security", "performance", "conventions"]
RC, AWC, AP = "REQUEST CHANGES", "APPROVE WITH COMMENTS", "APPROVE"

# (name, effort, input data, options, expected subset)
CASES = []


def add(name, effort, findings=(), ac=(), lanes=None, opts=None, **expect):
    data = {"findings": list(findings), "ac_matrix": list(ac),
            "lanes": L(*FULL) if lanes is None else lanes}
    CASES.append((name, effort, data, opts or {}, expect))


for lvl in ("high", "medium"):
    add(f"{lvl}: CRITICAL -> RC, hold_event default", lvl, [F("CRITICAL")], verdict=RC, event="REQUEST_CHANGES")
    add(f"{lvl}: CRITICAL, hold_event COMMENT", lvl, [F("CRITICAL")], opts={"hold_event": "COMMENT"},
        verdict=RC, event="COMMENT")
    add(f"{lvl}: Missing AC (named) -> RC", lvl, [F("HIGH", ac_ref="AC1")], [AC("AC1", "Missing")],
        verdict=RC, event="REQUEST_CHANGES", unsatisfied=1)
    add(f"{lvl}: Contradicted AC (named) -> RC", lvl, [F("MEDIUM", ac_ref="B2")], [AC("B2", "Contradicted")],
        verdict=RC, unsatisfied=1)
    add(f"{lvl}: breaking, no migration -> RC", lvl,
        [F("MEDIUM", breaking={"consumers": "c", "migration": ""})], verdict=RC)
    add(f"{lvl}: breaking, migration 'none' -> RC", lvl,
        [F("MEDIUM", breaking={"consumers": "c", "migration": "none"})], verdict=RC)
    add(f"{lvl}: breaking with migration, only that -> APPROVE WITH COMMENTS", lvl,
        [F("MEDIUM", breaking={"consumers": "c", "migration": "Call the v2 route."})], verdict=AWC)
    add(f"{lvl}: 4 HIGH -> RC", lvl, [F("HIGH") for _ in range(4)], verdict=RC, event="REQUEST_CHANGES")
    add(f"{lvl}: 3 HIGH -> APPROVE WITH COMMENTS", lvl, [F("HIGH") for _ in range(3)], verdict=AWC, event="COMMENT")
    add(f"{lvl}: 1 HIGH -> AWC", lvl, [F("HIGH")], verdict=AWC, event="COMMENT")
    add(f"{lvl}: MEDIUM -> AWC", lvl, [F("MEDIUM")], verdict=AWC, event="COMMENT")
    add(f"{lvl}: QUESTION -> AWC (table row; open point A1)", lvl, [F("QUESTION")], verdict=AWC, event="COMMENT")
    add(f"{lvl}: only LOW -> APPROVE", lvl, [F("LOW")], verdict=AP, event="APPROVE")
    add(f"{lvl}: nothing -> APPROVE", lvl, [], verdict=AP, event="APPROVE")
    add(f"{lvl}: Partial and Not verifiable ACs only -> APPROVE", lvl, [],
        [AC("AC1", "Partial"), AC("AC2", "Not verifiable"), AC("AC3", "Met")], verdict=AP, unsatisfied=0)
    add(f"{lvl}: pre-existing CRITICAL does not count", lvl,
        [F("CRITICAL", introduced_by_pr="pre-existing")], verdict=AP)
    add(f"{lvl}: refuted HIGH does not count", lvl, [F("HIGH", verified="refuted")], verdict=AP)
    add(f"{lvl}: author_is_me -> event COMMENT, ready phrases", lvl, [F("CRITICAL")],
        opts={"author_is_me": True}, verdict=RC, event="COMMENT", phrase="Not ready for review")
    add(f"{lvl}: author_is_me, nothing", lvl, [], opts={"author_is_me": True}, verdict=AP, event="COMMENT",
        phrase="Ready for review")
    add(f"{lvl}: phrases RC", lvl, [F("CRITICAL")], phrase="Not ready to merge", emoji="🔴")
    add(f"{lvl}: phrases AWC", lvl, [F("MEDIUM")], phrase="Ready to merge after the comments", emoji="🟡")
    add(f"{lvl}: phrases APPROVE", lvl, [], phrase="Ready to merge", emoji="🟢")
    # open counts
    add(f"{lvl}: open counts", lvl, [F("CRITICAL"), F("HIGH"), F("HIGH"), F("MEDIUM"), F("LOW"), F("QUESTION")],
        open={"critical": 1, "high": 2, "medium": 1, "low": 1, "question": 1})

# Team-gate band
add("band: security HIGH -> FAIL", "high", [F("HIGH", lane="security")], band="FAIL")
add("band: performance MEDIUM -> CONCERNS", "high", [F("MEDIUM", lane="performance")], band="CONCERNS")
add("band: code-quality LOW -> PASS", "high", [F("LOW", lane="code-quality")], band="PASS")
add("band: none -> PASS", "high", [], band="PASS")
add("band: QUESTION counts as none -> PASS (open point A8)", "high", [F("QUESTION", lane="security")], band="PASS")
add("band: worst wins", "high", [F("MEDIUM", lane="performance"), F("CRITICAL", lane="security"),
                                 F("LOW", lane="code-quality")], band="FAIL")
add("band: CRITICAL in adherence lane is not banded", "high", [F("CRITICAL", lane="adherence")], band="PASS")
add("band: legacy business-logic lane alias is still accepted", "high", [F("CRITICAL", lane="business-logic")],
    lanes=L("business-logic", "code-quality", "security", "performance", "conventions"), band="PASS")
add("band: NOT ASSESSED lane caps below PASS, complete false, verdict capped", "high", [],
    lanes=L("adherence", "code-quality", "performance", security="NOT ASSESSED"),
    band="CONCERNS", complete=False, verdict=AWC)
add("verdict: NOT ASSESSED with nothing open -> APPROVE WITH COMMENTS (incomplete)", "medium", [],
    lanes=L("adherence", "code-quality", conventions="NOT ASSESSED"), complete=False, verdict=AWC)
add("verdict: NOT ASSESSED never lowers REQUEST CHANGES", "high", [F("CRITICAL", lane="security")],
    lanes=L("adherence", "code-quality", security="NOT ASSESSED"), complete=False, verdict=RC)
add("band: NOT ASSESSED plus HIGH stays FAIL", "high", [F("HIGH", lane="code-quality")],
    lanes=L("adherence", "code-quality", security="NOT ASSESSED"), band="FAIL", complete=False)
add("band: no band lane ran -> n/a", "high", [], lanes=L("adherence", "conventions"), band="n/a")
add("band: abstained lane is returned, not failed", "high", [],
    lanes=L("adherence", "code-quality", security="abstained"), band="PASS", complete=True)
add("band: medium folded -> PASS (partial)", "medium", [], lanes=L("adherence", "code-quality", "conventions"),
    opts={}, band="PASS (partial)")
CASES[-1][2]["folded"] = ["security"]
add("band: medium folded + MEDIUM -> CONCERNS (partial)", "medium", [F("MEDIUM", lane="code-quality")],
    lanes=L("adherence", "code-quality", "conventions"), band="CONCERNS (partial)")
CASES[-1][2]["folded"] = ["performance", "security"]
add("band: medium folded + HIGH -> FAIL (partial)", "medium", [F("HIGH", lane="code-quality")],
    lanes=L("adherence", "code-quality", "conventions"), band="FAIL (partial)")
CASES[-1][2]["folded"] = ["security"]
add("band: medium nothing folded -> plain PASS", "medium", [], lanes=L("adherence", "code-quality", "security"),
    band="PASS")
add("band: NOT ASSESSED with MEDIUM stays APPROVE WITH COMMENTS", "high", [F("MEDIUM")],
    lanes=L("adherence", "code-quality", security="NOT ASSESSED"), verdict=AWC, complete=False)

# Low
add("low: CRITICAL", "low", [F("CRITICAL")], lanes=L("critical-risk"), verdict=RC, event="COMMENT",
    phrase="Not ready to merge", emoji="🔴", band="n/a")
add("low: Missing AC", "low", [F("HIGH", ac_ref="AC1")], [AC("AC1", "Missing")], lanes=L("critical-risk"),
    verdict=RC, event="COMMENT", phrase="Not ready to merge")
add("low: Contradicted AC only", "low", [F("MEDIUM", ac_ref="AC2")], [AC("AC2", "Contradicted")], lanes=[],
    verdict=RC, phrase="Not ready to merge")
add("low: HIGH only", "low", [F("HIGH")], lanes=[], verdict=RC, event="COMMENT",
    phrase="Fix the HIGH items before merge", emoji="🔴")
add("low: 4 HIGH is not special", "low", [F("HIGH") for _ in range(4)], lanes=[], phrase="Fix the HIGH items before merge")
add("low: nothing -> never APPROVE event", "low", [], lanes=[], verdict=AP, event="COMMENT", emoji="🟢",
    phrase="No CRITICAL or HIGH issues found. This review did not examine MEDIUM or LOW issues.")
add("low: QUESTION and MEDIUM are not examined", "low", [F("QUESTION"), F("MEDIUM")], lanes=[],
    verdict=AP, event="COMMENT")
add("low: breaking is not a separate row", "low", [F("MEDIUM", breaking={"consumers": "c", "migration": ""})],
    lanes=[], verdict=AP)
add("low: author_is_me", "low", [F("HIGH")], lanes=[], opts={"author_is_me": True}, event="COMMENT",
    phrase="Not ready for review")

# Skim
SK = L("skim")
add("skim: CRITICAL -> RC, hold_event", "skim", [F("CRITICAL")], lanes=SK, verdict=RC, event="REQUEST_CHANGES",
    phrase="Not ready to merge", band="n/a")
add("skim: CRITICAL, hold COMMENT", "skim", [F("CRITICAL")], lanes=SK, opts={"hold_event": "COMMENT"}, event="COMMENT")
add("skim: HIGH -> RC, COMMENT", "skim", [F("HIGH")], lanes=SK, verdict=RC, event="COMMENT")
add("skim: CRITICAL and HIGH -> hold_event", "skim", [F("HIGH"), F("CRITICAL")], lanes=SK, event="REQUEST_CHANGES")
add("skim: QUESTION -> AWC, COMMENT", "skim", [F("QUESTION")], lanes=SK, verdict=AWC, event="COMMENT",
    phrase="Ready to merge after the comments")
add("skim: nothing, in profile, CI passing -> APPROVE", "skim", [], lanes=SK,
    opts={"in_profile": True, "ci": "passing"}, verdict=AP, event="APPROVE", phrase="Ready to merge")
add("skim: nothing, over size -> COMMENT", "skim", [], lanes=SK, opts={"ci": "passing"}, verdict=AP, event="COMMENT")
add("skim: nothing, CI pending -> COMMENT", "skim", [], lanes=SK, opts={"in_profile": True, "ci": "pending"},
    event="COMMENT")
add("skim: nothing, CI none -> COMMENT", "skim", [], lanes=SK, opts={"in_profile": True, "ci": "none"}, event="COMMENT")
add("skim: nothing, CI failing -> COMMENT", "skim", [], lanes=SK,
    opts={"in_profile": True, "ci": "failing: build"}, event="COMMENT")
add("skim: nothing, CI unknown -> COMMENT", "skim", [], lanes=SK, opts={"in_profile": True}, event="COMMENT")
add("skim: author_is_me -> COMMENT, review phrases", "skim", [], lanes=SK,
    opts={"in_profile": True, "ci": "passing", "author_is_me": True}, event="COMMENT", phrase="Ready for review")
add("skim: author_is_me CRITICAL", "skim", [F("CRITICAL")], lanes=SK, opts={"author_is_me": True},
    event="COMMENT", phrase="Not ready for review")
add("skim: no AC matrix, rows ignored", "skim", [], [AC("AC1", "Missing")], lanes=SK,
    opts={"in_profile": True, "ci": "passing"}, verdict=AP, unsatisfied=0)
add("skim: MEDIUM must be dropped -> invalid", "skim", [F("MEDIUM")], lanes=SK, valid=False)
add("skim: lane failed -> no verdict, invalid", "skim", [], lanes=L(skim="NOT ASSESSED"), valid=False)

# Additive rules (new optional fields only)
add("new: no status counts as todo, no origin as new", "high", [F("HIGH")], open={"high": 1}, declined_blocking=0)
add("new: fixed finding is not open", "high", [F("CRITICAL", status="fixed", commit="abc1234")],
    verdict=AP, open={"critical": 0})
add("new: origin existing is not open", "high", [F("CRITICAL", origin="existing")], verdict=AP, open={"critical": 0})
add("new: declined new HIGH -> not APPROVE", "high", [F("HIGH", status="declined", reason="false positive")],
    verdict=AWC, event="COMMENT", declined_blocking=1, open={"high": 0})
add("new: declined new CRITICAL banded FAIL", "high", [F("CRITICAL", lane="security", status="declined", reason="r")],
    band="FAIL", declined_blocking=1)
add("new: declined MEDIUM is not blocking", "high", [F("MEDIUM", status="declined", reason="r")], verdict=AP,
    declined_blocking=0)
add("new: declined existing HIGH is not blocking", "high",
    [F("HIGH", status="declined", reason="r", origin="existing")], verdict=AP, declined_blocking=0)
add("new: declined plus open CRITICAL stays RC", "high",
    [F("HIGH", status="declined", reason="r"), F("CRITICAL")], verdict=RC)
add("new: skim declined blocks APPROVE", "skim", [F("HIGH", status="declined", reason="r")], lanes=SK,
    opts={"in_profile": True, "ci": "passing"}, verdict=AWC, event="COMMENT")
add("new: low declined blocks APPROVE", "low", [F("HIGH", status="declined", reason="r")], lanes=[],
    verdict=AWC, event="COMMENT")

# Invalid inputs
add("invalid: Missing AC with no naming finding", "high", [], [AC("AC1", "Missing")], valid=False, verdict=RC)
add("invalid: Contradicted AC named by another ref", "high", [F("HIGH", ac_ref="AC2")], [AC("AC1", "Contradicted")],
    valid=False)
add("valid: ac_ref lists several refs", "high", [F("HIGH", ac_ref="AC1, B2")], [AC("B2", "Missing")], valid=True)
add("invalid: HIGH with no trigger", "high", [F("HIGH", trigger="")], valid=False)
add("invalid: HIGH with n/a trigger", "high", [F("HIGH", trigger="n/a")], valid=False)
add("valid: conventions HIGH with n/a (convention)", "high", [F("HIGH", lane="conventions", trigger="n/a (convention)")],
    valid=True)
add("invalid: unverified HIGH not downgraded", "high", [F("HIGH", verified="unverified")], valid=False)
add("invalid: bad severity", "high", [F("BLOCKER")], valid=False)
add("invalid: bad status", "high", [F("LOW", status="open")], valid=False)
add("invalid: bad origin", "high", [F("LOW", origin="old")], valid=False)
add("invalid: fixed without commit", "high", [F("LOW", status="fixed")], valid=False)
add("invalid: declined without reason", "high", [F("LOW", status="declined")], valid=False)
add("invalid: duplicate ids", "high", [F("LOW", id="D1"), F("LOW", id="D1")], valid=False)
add("invalid: lane NOT ASSESSED needs a reason", "high", [], lanes=[{"lane": "security", "status": "NOT ASSESSED"}],
    valid=False)
add("invalid: unknown lane status", "high", [], lanes=[{"lane": "security", "status": "done"}], valid=False)
add("invalid: lanes missing at high", "high", [], lanes=None, valid=True)
CASES[-1][2].pop("lanes")
CASES[-1][4]["valid"] = False
add("invalid: challenges unknown id", "high", [F("LOW", challenges="Z9")], valid=False)
add("valid: challenges a declined id", "high",
    [F("MEDIUM", id="Z9", status="declined", reason="r"), F("HIGH", challenges="Z9")], valid=True)
add("invalid: cleared without evidence", "high", [], lanes=[{"lane": "security", "cleared": [{"id": "B1"}]}],
    valid=False)
add("eval: evaluation lane returned -> valid", "high", [F("MEDIUM", eval_disposition="post")],
    lanes=L(*FULL, "evaluation"), valid=True, verdict=AWC)
add("eval: evaluation lane abstained -> complete", "high", [],
    lanes=L(*FULL, evaluation="abstained"), valid=True, complete=True, verdict=AP)
add("eval: evaluation lane NOT ASSESSED -> incomplete", "high", [],
    lanes=L(*FULL, evaluation="NOT ASSESSED"), valid=True, complete=False, verdict=AWC)
add("eval: chat-only and drop excluded from open counts", "high",
    [F("MEDIUM", eval_disposition="chat-only"), F("LOW", eval_disposition="drop")],
    lanes=L(*FULL, "evaluation"), valid=True, verdict=AP, event="APPROVE",
    open={"critical": 0, "high": 0, "medium": 0, "low": 0, "question": 0})
add("eval: invalid eval_disposition rejected", "high", [F("MEDIUM", eval_disposition="maybe")], valid=False)
add("eval: eval_downgraded without eval_reason rejected", "high", [F("MEDIUM", eval_downgraded=True)], valid=False)


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
    o = breaker.compute({"findings": post_ok, "lanes": base_lanes}, {}, pre=pre)
    chk("floor: kept by title and by merged_titles/location", o["valid"], str(o["invalid"]))
    post_drop = [post_ok[0]]
    o = breaker.compute({"findings": post_drop, "lanes": base_lanes}, {}, pre=pre)
    chk("floor: dropped HIGH is invalid", not o["valid"] and "safety floor" in o["invalid"][0], str(o["invalid"]))
    post_low = [post_ok[0], F("MEDIUM", id="M1", title="Missing check", location="b.py:5")]
    o = breaker.compute({"findings": post_low, "lanes": base_lanes}, {}, pre=pre)
    chk("floor: lowered HIGH is invalid", not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))
    pre_unv = [F("HIGH", id="x", title="Not confirmed", verified="unverified")]
    o = breaker.compute({"findings": [], "lanes": base_lanes}, {}, pre=pre_unv)
    chk("floor: unverified pre finding is not on the floor", o["valid"], str(o["invalid"]))
    o = breaker.compute({"findings": [F("HIGH", id="H1", title="Other", location="zz.py:1",
                                         introduced_by_pr="pre-existing")], "lanes": base_lanes}, {},
                        pre=[F("HIGH", id="q", title="Other", location="zz.py:1")])
    chk("floor: moved to pre-existing is kept, not dropped", o["valid"], str(o["invalid"]))
    # evaluation lane safety floor vs non-floor downgrade
    cq_pre = [F("HIGH", id="cq-1", lane="code-quality", title="Verbose helper", location="c.py:10")]
    cq_down_ok = [F("MEDIUM", id="D1", lane="code-quality", title="Verbose helper", location="c.py:10",
                    eval_disposition="post", eval_downgraded=True,
                    eval_reason="Readability issue does not crash on main path.")]
    o = breaker.compute({"findings": cq_down_ok, "lanes": base_lanes + [{"lane": "evaluation"}]}, {}, pre=cq_pre)
    chk("eval floor: non-floor HIGH downgraded with eval_reason is valid", o["valid"], str(o["invalid"]))

    crit_pre = [F("CRITICAL", id="sec-c", lane="security", title="SQL injection", location="d.py:4")]
    crit_down = [F("MEDIUM", id="S1", lane="security", title="SQL injection", location="d.py:4",
                   eval_disposition="post", eval_downgraded=True, eval_reason="try lower")]
    o = breaker.compute({"findings": crit_down, "lanes": base_lanes + [{"lane": "evaluation"}]}, {}, pre=crit_pre)
    chk("eval floor: confirmed CRITICAL cannot be downgraded", not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))

    sec_pre = [F("HIGH", id="sec-h", lane="security", title="Missing auth", location="d.py:8")]
    sec_down = [F("MEDIUM", id="S2", lane="security", title="Missing auth", location="d.py:8",
                  eval_disposition="post", eval_downgraded=True, eval_reason="try lower")]
    o = breaker.compute({"findings": sec_down, "lanes": base_lanes + [{"lane": "evaluation"}]}, {}, pre=sec_pre)
    chk("eval floor: confirmed security HIGH cannot be downgraded", not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))

    ac_pre = [F("HIGH", id="b-h", lane="adherence", title="Missing requirement", location="e.py:3", ac_ref="AC1")]
    ac_down = [F("MEDIUM", id="B1", lane="adherence", title="Missing requirement", location="e.py:3",
                 eval_disposition="post", eval_downgraded=True, eval_reason="try lower")]
    o = breaker.compute({"findings": ac_down, "lanes": base_lanes + [{"lane": "evaluation"}]}, {}, pre=ac_pre)
    chk("eval floor: confirmed ac_ref HIGH cannot be downgraded", not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))

    brk_pre = [F("HIGH", id="a-h", lane="compatibility", title="Removed field", location="f.py:2",
                 breaking={"consumers": "mobile", "migration": "none"})]
    brk_down = [F("MEDIUM", id="A1", lane="compatibility", title="Removed field", location="f.py:2",
                  eval_disposition="post", eval_downgraded=True, eval_reason="try lower")]
    o = breaker.compute({"findings": brk_down, "lanes": base_lanes + [{"lane": "evaluation"}]}, {}, pre=brk_pre)
    chk("eval floor: confirmed unmigrated breaking HIGH cannot be downgraded",
        not o["valid"] and "lowered" in " ".join(o["invalid"]), str(o["invalid"]))

    for disp in ("drop", "chat-only"):
        cq_hidden = [F("HIGH", id="D2", lane="code-quality", title="Verbose helper", location="c.py:10",
                       eval_disposition=disp, eval_reason="hide")]
        o = breaker.compute({"findings": cq_hidden, "lanes": base_lanes + [{"lane": "evaluation"}]}, {}, pre=cq_pre)
        chk(f"eval floor: confirmed HIGH cannot be {disp}",
            not o["valid"] and "missing" in " ".join(o["invalid"]), str(o["invalid"]))

        crit_hidden = [F("CRITICAL", id="S9", lane="security", title="SQL injection", location="d.py:4",
                         eval_disposition=disp, eval_reason="hide")]
        o = breaker.compute({"findings": crit_hidden, "lanes": base_lanes + [{"lane": "evaluation"}]}, {}, pre=crit_pre)
        chk(f"eval floor: confirmed CRITICAL cannot be {disp}",
            not o["valid"] and "missing" in " ".join(o["invalid"]), str(o["invalid"]))
    # previous carry
    prev = [F("HIGH", id="S2", status="todo", origin="new"),
            F("CRITICAL", id="B1", status="todo", origin="new"),
            F("MEDIUM", id="B3", status="todo", origin="new")]
    cur = [F("HIGH", id="S2", status="todo"), F("CRITICAL", id="B1", status="fixed", commit="abc1234")]
    o = breaker.compute({"findings": cur, "lanes": base_lanes}, {}, previous=prev)
    chk("previous: carried and fixed are accepted", o["valid"], str(o["invalid"]))
    o = breaker.compute({"findings": [cur[0]], "lanes": base_lanes}, {}, previous=prev)
    chk("previous: dropped CRITICAL is invalid", not o["valid"] and "B1" in o["invalid"][0], str(o["invalid"]))
    lanes_cleared = base_lanes + [{"lane": "robustness", "cleared": [{"id": "B1", "evidence": "a.py:3 guards it"}]}]
    o = breaker.compute({"findings": [cur[0]], "lanes": lanes_cleared}, {}, previous=prev)
    chk("previous: cleared with evidence is accepted", o["valid"], str(o["invalid"]))
    cur_low = [F("MEDIUM", id="S2", status="todo"), cur[1]]
    o = breaker.compute({"findings": cur_low, "lanes": base_lanes}, {}, previous=prev)
    chk("previous: lowered HIGH is invalid", not o["valid"] and "S2" in o["invalid"][0], str(o["invalid"]))
    legacy_prev = [F("HIGH", id="H1")]
    o = breaker.compute({"findings": [], "lanes": base_lanes}, {}, previous=legacy_prev)
    chk("previous: legacy file (no status) is skipped with a note", o["valid"] and o["notes"], str(o))
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

        good = write("good.json", {"findings": [F("MEDIUM")], "lanes": L(*FULL)})
        r = run(good)
        j = json.loads(r.stdout)
        res.append(("cli: valid input exits 0, JSON has the contract keys",
                    [] if r.returncode == 0 and all(k in j for k in
                     ("open", "unsatisfied", "complete", "band", "verdict", "event", "invalid")) and r.stderr == ""
                    else [f"rc={r.returncode} stderr={r.stderr!r}"]))
        bad = write("bad.json", {"findings": [], "ac_matrix": [AC("AC1", "Missing")], "lanes": L(*FULL)})
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
        skim = write("skim.json", {"findings": [], "lanes": L("skim")})
        r = run(skim, "--effort", "skim", "--in-profile", "--ci", "passing")
        res.append(("cli: skim flags give APPROVE", [] if json.loads(r.stdout)["event"] == "APPROVE"
                    else [r.stdout]))
        r = run(skim, "--effort", "skim", "--in-profile", "--ci", "passing", "--author-is-me")
        res.append(("cli: --author-is-me gives COMMENT", [] if json.loads(r.stdout)["event"] == "COMMENT"
                    else [r.stdout]))
        bare = write("bare.json", [F("LOW")])
        r = run(bare, "--effort", "low")
        res.append(("cli: bare array input works at low", [] if r.returncode == 0 and json.loads(r.stdout)["verdict"] == AP
                    else [f"rc={r.returncode} {r.stderr}"]))

        # CODEBASE level cases
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
