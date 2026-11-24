#!/usr/bin/env python3
"""Self-test for scripts/carry_findings.py. Run: python3 -I scripts/tests/carry/run_tests.py

The test runs the script as a subprocess, so it checks the CLI and the exit codes.
Fixtures are hand-written JSON in this folder. Python standard library only.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "..", "carry_findings.py"))
FAILS = []


def run(*args, expect=0):
    p = subprocess.run([sys.executable, "-I", SCRIPT, *args], capture_output=True, text=True)
    if (p.returncode == 0) != (expect == 0):
        FAILS.append(f"exit {p.returncode} for {args[:1]}: {p.stderr.strip()[:200]}")
    return p


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        FAILS.append(name)


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def jdump(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def by_id(doc):
    return {f["id"]: f for f in doc["findings"]}


def finding(lane, sev, loc, title, issue, **kw):
    f = {"id": "x", "lane": lane, "severity": sev, "confidence": "HIGH", "title": title,
         "location": loc, "issue": issue, "fix": "Fix it."}
    f.update(kw)
    return f


with tempfile.TemporaryDirectory() as tmp:
    prev1 = os.path.join(HERE, "prev_round1.json")
    new2 = os.path.join(HERE, "new_round2.json")

    # ---- prepare: declines.json, open.json, previous.json, next_id, PII
    r = run("prepare", prev1, tmp)
    s = json.loads(r.stdout)
    dec, opn = jload(os.path.join(tmp, "declines.json")), jload(os.path.join(tmp, "open.json"))
    check("prepare: declines are the 4 declined findings", sorted(d["id"] for d in dec) == ["P1", "S2", "S3", "V1"], dec)
    check("prepare: open is the todo CRITICAL/HIGH only", sorted(o["id"] for o in opn) == ["B1", "B2", "S1"], opn)
    check("prepare: decline shape is exactly the contract",
          all(set(d) == {"id", "file", "lines", "severity", "issue", "reason"} for d in dec))
    check("prepare: open shape is exactly the contract",
          all(set(o) == {"id", "lanes", "file", "lines", "severity", "issue"} for o in opn))
    check("prepare: email and handle redacted",
          "<email>" in dec[0]["reason"] and "<handle>" in dec[1]["reason"], dec)
    check("prepare: next_id counts ids in the previous list (S7 -> 8)", s["next_id"]["S"] == 8, s["next_id"])
    check("prepare: previous.json holds no raw email",
          "owner@example.com" not in open(os.path.join(tmp, "previous.json")).read())

    # ---- reconcile: every rule in one run
    out_path, rep_path = os.path.join(tmp, "out.json"), os.path.join(tmp, "rep.json")
    r = run("reconcile", os.path.join(tmp, "previous.json"), new2, out_path, "--reported", rep_path)
    summ, out = json.loads(r.stdout), jload(out_path)
    f = by_id(out)
    check("keep: raised-again todo keeps its id", f["B1"]["status"] == "todo" and f["B1"]["location"].endswith(":124"))
    check("reopen: raised-again fix keeps id and records reopened_from",
          f["D1"]["status"] == "todo" and f["D1"]["reopened_from"].startswith("a1b2c3d4"), f.get("D1"))
    check("repeat decline (MEDIUM) is dropped, decline carried",
          summ["dropped_repeat_decline"] == ["S2"] and f["S2"]["status"] == "declined"
          and "public id" in f["S2"]["reason"], summ["dropped_repeat_decline"])
    check("challenge to a MEDIUM decline: evidence recorded, stays declined, finding dropped",
          f["P1"]["status"] == "declined" and "bulk.py:20" in f["P1"]["challenge"]
          and not any(x["issue"].startswith("The bulk path") for x in out["findings"]), f.get("P1"))
    check("challenge to a HIGH decline: returns to todo with evidence",
          f["S3"]["status"] == "todo" and "middleware.py:12" in f["S3"]["challenge"], f.get("S3"))
    check("safety floor: verified HIGH repeating a decline with no challenge stays declined and is listed",
          f["V1"]["status"] == "declined" and "V1" in summ["declined_still_present"]
          and "V1" not in summ["reopened_decline"] and "V1" not in summ["dropped_repeat_decline"]
          and f["V1"]["reason"] == "The export is for internal use only."
          and "V1" not in summ["reportable_ids"], (f.get("V1"), summ.get("declined_still_present")))
    check("reopened decline takes the challenger location, not the stale one",
          f["S3"]["location"] == "backend/auth/session.py:91", f["S3"]["location"])
    check("every challenged decline is in the summary list", summ["challenged"] == ["P1", "S3"], summ["challenged"])
    check("carried open HIGH (not raised, cleared with no evidence) stays todo, unchanged",
          f["B2"]["status"] == "todo" and f["B2"]["location"] == "backend/orders/api.py:44"
          and "B2" in summ["carried_open"], f.get("B2"))
    check("cleared HIGH with evidence leaves the findings and goes to cleared",
          "S1" not in f and [c["id"] for c in out["cleared"]] == ["S1"]
          and "tokens.py:34" in out["cleared"][0]["evidence"], out["cleared"])
    check("cleared entry for a finding raised again is ignored", "B1" in f and "B1" not in [c["id"] for c in out["cleared"]])
    check("still_open MEDIUM carried; other MEDIUM not carried",
          "A1" in f and "R1" not in f and summ["not_carried"] == ["R1"], summ["not_carried"])
    check("fixed finding not raised stays fixed with its commit", f["D2"]["status"] == "fixed" and f["D2"]["commit"].startswith("b1b2"))
    check("new findings get fresh ids from next_id (D3, S8), never a reused one",
          sorted(summ["new"]) == ["D3", "S8"] and out["next_id"]["S"] == 9 and out["next_id"]["D"] == 4, summ["new"])
    check("ids are unique", len(out["findings"]) == len({x["id"] for x in out["findings"]}))
    check("todo findings carry a display label and others do not",
          all(("display_id" in x) == (x["status"] == "todo") for x in out["findings"]))
    check("display labels are per severity and unique",
          len({x["display_id"] for x in out["findings"] if "display_id" in x}) == sum(1 for x in out["findings"] if x["status"] == "todo"))
    check("reported file holds todo findings only", {x["id"] for x in jload(rep_path)} == set(summ["reportable_ids"])
          and all(x["status"] == "todo" for x in jload(rep_path)))
    check("last_sha and previous pass through", out["last_sha"] == "2" * 40 and out["previous"] == [])
    check("every previous CRITICAL/HIGH todo is raised, cleared or carried",
          all(i in f or i in [c["id"] for c in out["cleared"]] for i in ("B1", "B2", "S1")))

    # ---- round 3: nothing raised. State carries; reopened_from survives.
    new3 = {"pr": out["pr"], "last_sha": "3" * 40, "findings": [
        finding("adherence", "MEDIUM", "backend/orders/service.py:204", "Split rounding drops one cent",
                "Splitting an amount in three parts drops one cent through rounding.")]}
    jdump(os.path.join(tmp, "new3.json"), new3)
    r = run("reconcile", out_path, os.path.join(tmp, "new3.json"), os.path.join(tmp, "out3.json"))
    f3 = by_id(jload(os.path.join(tmp, "out3.json")))
    check("round 3: D1 keeps reopened_from and id", f3["D1"]["reopened_from"].startswith("a1b2c3d4") and f3["D1"]["status"] == "todo")
    check("round 3: declines and fixes still carried",
          f3["P1"]["status"] == "declined" and f3["S2"]["status"] == "declined" and f3["D2"]["status"] == "fixed")
    check("round 3: open HIGH carried again (a miss never closes it)", f3["B2"]["status"] == "todo")
    check("round 3: next_id never goes down", jload(os.path.join(tmp, "out3.json"))["next_id"]["S"] == 9)

    # ---- lead-resolved finding becomes fixed with the head sha, then reopens
    new_res = {"pr": out["pr"], "last_sha": "4" * 40, "findings": [],
               "cleared": [{"id": "B2", "evidence": "backend/orders/api.py:46 now has the permission class.", "resolved": True, "lane": "lead"}]}
    jdump(os.path.join(tmp, "new_res.json"), new_res)
    run("reconcile", out_path, os.path.join(tmp, "new_res.json"), os.path.join(tmp, "out_res.json"))
    fr = by_id(jload(os.path.join(tmp, "out_res.json")))
    check("resolved clear: finding carried as fixed with commit = new last_sha",
          fr["B2"]["status"] == "fixed" and fr["B2"]["commit"] == "4" * 40, fr.get("B2"))
    new_back = {"pr": out["pr"], "last_sha": "5" * 40, "findings": [
        finding("security", "HIGH", "backend/orders/api.py:44", "Export has no permission check",
                "The export endpoint does not check the caller permission.", trigger="Any user calls it.", verified="confirmed")]}
    jdump(os.path.join(tmp, "new_back.json"), new_back)
    run("reconcile", os.path.join(tmp, "out_res.json"), os.path.join(tmp, "new_back.json"), os.path.join(tmp, "out_back.json"))
    fb = by_id(jload(os.path.join(tmp, "out_back.json")))
    check("regression of a lead-resolved finding reopens with the same id",
          fb["B2"]["status"] == "todo" and fb["B2"]["reopened_from"] == "4" * 40, fb.get("B2"))

    # ---- first run: no previous
    r = run("prepare", "-", tmp + "/does-not-matter")
    s0 = json.loads(r.stdout)
    check("first run: prepare writes nothing and reports none", s0["previous"] == "none" and s0["next_id"] == {} and s0["files"] == [])
    first_new = {"findings": [finding("security", "HIGH", "a/b.py:3", "Bad thing", "A bad thing happens here.", trigger="Input x."),
                              finding("security", "LOW", "a/c.py:9", "Small thing", "A small thing happens there."),
                              finding("conventions", "QUESTION", "a/d.py:1", "Which clients?", "The text does not say which clients read it.")]}
    jdump(os.path.join(tmp, "first.json"), first_new)
    r = run("reconcile", "-", os.path.join(tmp, "first.json"), os.path.join(tmp, "first_out.json"))
    fo = jload(os.path.join(tmp, "first_out.json"))
    check("first run: ids start at 1 per lane letter, all todo, nothing carried",
          [x["id"] for x in fo["findings"]] == ["S1", "S2", "V1"] and all(x["status"] == "todo" for x in fo["findings"])
          and fo["next_id"] == {"S": 3, "V": 2} and fo["reconcile"]["counts"]["carried_open"] == 0, fo["next_id"])
    check("first run: finding content unchanged", fo["findings"][0]["issue"] == "A bad thing happens here."
          and fo["findings"][0]["trigger"] == "Input x.")
    check("first run: id letters avoid C H M L Q", all(x["id"][0] not in "CHMLQ" for x in fo["findings"]))

    # ---- legacy previous file (old shape, severity-letter ids, no status)
    legacy = os.path.join(HERE, "prev_legacy.json")
    lt = os.path.join(tmp, "legacy")
    os.mkdir(lt)
    r = run("prepare", legacy, lt)
    sl = json.loads(r.stdout)
    pl = jload(os.path.join(lt, "previous.json"))
    check("legacy: severity-letter ids are remapped to lane-letter ids",
          sl["id_map"] == {"H1": "X1", "M1": "S1", "Q1": "X2"} and [x["id"] for x in pl["findings"]] == ["X1", "S1", "X2"], sl["id_map"])
    check("legacy: missing status reads as todo; HIGH goes to open.json with title as issue",
          jload(os.path.join(lt, "open.json"))[0]["issue"] == "Charge retried without idempotency key"
          and all(x["status"] == "todo" for x in pl["findings"]))
    r = run("prepare", os.path.join(lt, "previous.json"), lt)
    check("normalising twice changes nothing (idempotent)", json.loads(r.stdout)["id_map"] == {}
          and [x["id"] for x in jload(os.path.join(lt, "previous.json"))["findings"]] == ["X1", "S1", "X2"])

    # ---- ambiguity is new
    amb_prev = {"findings": [
        {"id": "S1", "lane": "security", "severity": "MEDIUM", "title": "Unchecked length", "location": "m/v.py:10",
         "issue": "The view reads the length field without a limit.", "status": "todo"},
        {"id": "S2", "lane": "security", "severity": "MEDIUM", "title": "Unchecked length", "location": "m/v.py:12",
         "issue": "The view reads the length field without a limit.", "status": "todo"}], "next_id": {"S": 3}}
    jdump(os.path.join(tmp, "amb_prev.json"), amb_prev)
    jdump(os.path.join(tmp, "amb_new.json"), {"last_sha": "6" * 40, "findings": [
        finding("security", "MEDIUM", "m/v.py:11", "Unchecked length", "The view reads the length field without a limit.")]})
    r = run("reconcile", os.path.join(tmp, "amb_prev.json"), os.path.join(tmp, "amb_new.json"), os.path.join(tmp, "amb_out.json"))
    sa = json.loads(r.stdout)
    check("ambiguous match is treated as new (S3), never a guess", sa["new"] == ["S3"] and len(sa["ambiguous"]) == 1, sa)

    # ---- severity: a lower re-score never softens an open HIGH
    sev_prev = {"findings": [{"id": "B1", "lane": "adherence", "severity": "HIGH", "title": "Refund wrong",
                              "location": "r/a.py:5", "issue": "The refund uses the wrong total after a partial capture.", "status": "todo"}],
                "next_id": {"B": 2}}
    jdump(os.path.join(tmp, "sev_prev.json"), sev_prev)
    jdump(os.path.join(tmp, "sev_new.json"), {"last_sha": "7" * 40, "findings": [
        finding("adherence", "MEDIUM", "r/a.py:6", "Refund wrong", "The refund uses the wrong total after a partial capture.")]})
    run("reconcile", os.path.join(tmp, "sev_prev.json"), os.path.join(tmp, "sev_new.json"), os.path.join(tmp, "sev_out.json"))
    check("severity of an open HIGH is kept when the new review is lower",
          by_id(jload(os.path.join(tmp, "sev_out.json")))["B1"]["severity"] == "HIGH")

    # ---- unverified HIGH repeating a decline is dropped; refuted input is excluded
    dec_prev = {"findings": [{"id": "S1", "lane": "security", "severity": "HIGH", "title": "Header trusted",
                              "location": "r/h.py:5", "issue": "The code trusts the forwarded header for the client address.",
                              "status": "declined", "reason": "The proxy strips the header."}], "next_id": {"S": 2}}
    jdump(os.path.join(tmp, "dec_prev.json"), dec_prev)
    jdump(os.path.join(tmp, "dec_new.json"), {"last_sha": "8" * 40, "findings": [
        finding("security", "HIGH", "r/h.py:5", "Header trusted", "The code trusts the forwarded header for the client address.", trigger="Forged header."),
        finding("security", "HIGH", "r/z.py:5", "Other", "Something else entirely happens in this file.", verified="refuted")]})
    r = run("reconcile", os.path.join(tmp, "dec_prev.json"), os.path.join(tmp, "dec_new.json"), os.path.join(tmp, "dec_out.json"))
    sd = json.loads(r.stdout)
    check("unverified repeat of a decline is dropped; refuted input excluded",
          sd["dropped_repeat_decline"] == ["S1"] and sd["new"] == [] and by_id(jload(os.path.join(tmp, "dec_out.json")))["S1"]["status"] == "declined", sd)

    # ---- reworded text: real lanes reword each round. Print the scores. Do not tune thresholds to pass.
    import importlib.util
    spec = importlib.util.spec_from_file_location("carry_findings_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cases = [
        ("open HIGH reworded, 3 lines lower",
         {"id": "B1", "lane": "adherence", "severity": "HIGH", "status": "todo", "title": "Refund ignores partial captures",
          "location": "r/svc.py:120", "issue": "The refund total uses the full order amount and ignores partial captures."},
         finding("adherence", "HIGH", "r/svc.py:123", "Partial capture not deducted from refund",
                 "When a payment is only partly captured, the refund still pays back the whole amount of the order.",
                 trigger="Capture 40 of 100, then refund.", verified="confirmed"), mod.SIM_NEAR),
        ("open HIGH lightly reworded",
         {"id": "B1", "lane": "adherence", "severity": "HIGH", "status": "todo", "title": "Refund ignores partial captures",
          "location": "r/svc.py:120", "issue": "The refund total uses the full order amount and ignores partial captures."},
         finding("adherence", "HIGH", "r/svc.py:122", "Refund ignores partial capture",
                 "The refund total takes the full order amount and ignores a partial capture.",
                 trigger="Capture 40 of 100, then refund.", verified="confirmed"), mod.SIM_NEAR),
        ("decline reworded, same line",
         {"id": "S2", "lane": "security", "severity": "MEDIUM", "status": "declined", "reason": "Public id.", "title": "Order reference logged in clear text",
          "location": "r/log.py:15", "issue": "The order reference is written to the log in clear text."},
         finding("security", "MEDIUM", "r/log.py:15", "Order id in the log",
                 "The code logs the order reference as plain text, so anyone with log access can read it."), mod.SIM_NEAR_DECLINE),
        ("decline lightly reworded",
         {"id": "S2", "lane": "security", "severity": "MEDIUM", "status": "declined", "reason": "Public id.", "title": "Order reference logged in clear text",
          "location": "r/log.py:15", "issue": "The order reference is written to the log in clear text."},
         finding("security", "MEDIUM", "r/log.py:16", "Order reference written to log in clear text",
                 "The order reference goes to the log in clear text."), mod.SIM_NEAR_DECLINE),
    ]
    print("\nOVERLAP SCORES (reworded text; threshold in brackets)")
    for name, old_f, new_f, need in cases:
        sc = mod.similarity(new_f, old_f)
        print(f"  {name}: overlap {sc:.2f} [need {need:.2f}] -> {'match' if sc >= need else 'no match'}")
        pv = {"findings": [old_f], "next_id": {old_f['id'][0]: 3}}
        jdump(os.path.join(tmp, "rw_prev.json"), pv)
        jdump(os.path.join(tmp, "rw_new.json"), {"last_sha": "9" * 40, "findings": [new_f]})
        rr = run("reconcile", os.path.join(tmp, "rw_prev.json"), os.path.join(tmp, "rw_new.json"), os.path.join(tmp, "rw_out.json"))
        so = json.loads(rr.stdout)
        ro = jload(os.path.join(tmp, "rw_out.json"))
        # safe either way: an open HIGH stays visible once; a decline is dropped or comes back as a new finding
        if old_f["status"] == "todo":
            todo_n = sum(1 for x in ro["findings"] if x["status"] == "todo")
            check(f"reworded: {name}: HIGH stays visible (kept, or new plus carried)",
                  (so["kept"] == ["B1"] and todo_n == 1) or (so["carried_open"] == ["B1"] and len(so["new"]) == 1 and todo_n == 2), so)
        else:
            check(f"reworded: {name}: decline dropped, or the finding returns as new",
                  (so["dropped_repeat_decline"] == ["S2"]) or (len(so["new"]) == 1), so)

    # ---- mark
    marks = os.path.join(tmp, "marks.json")
    jdump(marks, [{"id": "P1", "status": "todo"}, {"id": "D1", "status": "declined", "reason": "Not a bug, per owner@example.com."},
                  {"id": "S3", "status": "fixed", "commit": "abc1234"}])
    cp = os.path.join(tmp, "marked.json")
    jdump(cp, out)
    r = run("mark", cp, marks)
    fm = by_id(jload(cp))
    check("mark: sets declined with redacted reason, fixed with commit, todo",
          fm["D1"]["status"] == "declined" and "<email>" in fm["D1"]["reason"] and fm["S3"]["status"] == "fixed"
          and fm["S3"]["commit"] == "abc1234" and fm["P1"]["status"] == "todo" and "display_id" not in fm["D1"], fm["D1"])
    jdump(marks, [{"id": "D1", "status": "declined"}])
    run("mark", cp, marks, expect=2)
    jdump(marks, [{"id": "D1", "status": "fixed"}])
    run("mark", cp, marks, expect=2)
    jdump(marks, [{"id": "NOPE", "status": "todo"}])
    run("mark", cp, marks, expect=2)

    # ---- bad input exits non-zero with a message
    bad = os.path.join(tmp, "bad.json")
    open(bad, "w").write("{not json")
    for name, args in [
        ("invalid JSON", ("prepare", bad, tmp)),
        ("missing previous file", ("prepare", os.path.join(tmp, "nope.json"), tmp)),
        ("missing new file", ("reconcile", prev1, os.path.join(tmp, "nope.json"), os.path.join(tmp, "o.json"))),
        ("bad usage", ("reconcile", prev1)),
        ("unknown command", ("frobnicate",)),
    ]:
        p = run(*args, expect=2)
        check(f"bad input: {name} exits non-zero with a message", p.returncode != 0 and "carry_findings" in p.stderr, p.stderr)
    badstat = copy.deepcopy(jload(prev1))
    badstat["findings"][0]["status"] = "resolved"
    jdump(os.path.join(tmp, "badstat.json"), badstat)
    p = run("prepare", os.path.join(tmp, "badstat.json"), tmp, expect=2)
    check("bad input: invalid status named in the message", "invalid status" in p.stderr and p.returncode != 0, p.stderr)
    dup = copy.deepcopy(jload(prev1))
    dup["findings"][1]["id"] = "B1"
    jdump(os.path.join(tmp, "dup.json"), dup)
    p = run("prepare", os.path.join(tmp, "dup.json"), tmp, expect=2)
    check("bad input: duplicate id rejected", "duplicate id" in p.stderr, p.stderr)
    nosev = copy.deepcopy(jload(new2))
    del nosev["findings"][0]["severity"]
    jdump(os.path.join(tmp, "nosev.json"), nosev)
    p = run("reconcile", prev1, os.path.join(tmp, "nosev.json"), os.path.join(tmp, "o.json"), expect=2)
    check("bad input: finding without severity rejected", "severity" in p.stderr, p.stderr)

    # ---- the script writes no names: scan every output for the fixture email and handle
    blob = ""
    for root, _d, files in os.walk(tmp):
        for fn in files:
            if fn.endswith(".json") and fn not in ("prev_round1.json",):
                blob += open(os.path.join(root, fn), encoding="utf-8").read()
    # mark's input file and the prev_round1 copy are inputs; check outputs only
    outputs = "".join(open(os.path.join(tmp, n)).read() for n in ("declines.json", "open.json", "previous.json", "out.json", "out3.json"))
    check("no email or handle in any output file", "owner@example.com" not in outputs and "@reviewer-handle" not in outputs)

    # ---- Wave 2 regression coverage: carry -> breaker integration, challenges, nulls, PII, CODEBASE, mark
    breaker_path = os.path.normpath(os.path.join(HERE, "..", "..", "breaker.py"))
    bspec = importlib.util.spec_from_file_location("breaker_mod", breaker_path)
    bmod = importlib.util.module_from_spec(bspec)
    bspec.loader.exec_module(bmod)

    # 1. prev_round1 + new_round2 (declined_still_present V1/sec-6) passes breaker --pre & --previous
    n2_raw = jload(new2)
    n2_pre = [dict(x, verified="confirmed" if x["severity"] in ("CRITICAL", "HIGH") and not x.get("verified") else x.get("verified"))
              for x in n2_raw["findings"]]
    b_lanes = [{"lane": x} for x in ("adherence", "code-quality", "security", "performance", "conventions")]
    for cl in out["cleared"]:
        b_lanes.append({"lane": cl.get("lane", "adherence"), "status": "returned", "cleared": [cl]})
    b_res = bmod.compute({"findings": out["findings"], "lanes": b_lanes}, {"effort": "high"},
                         pre=n2_pre, previous=jload(os.path.join(tmp, "previous.json"))["findings"])
    check("carry->breaker: prev_round1 + new_round2 (declined_still_present) passes safety floor",
          b_res["valid"] and b_res["declined_blocking"] == 1, (b_res["declined_blocking"], b_res["invalid"]))

    # 2. HIGH challenging MEDIUM decline reopens it with full metadata and passes breaker safety floor
    med_dec_prev = os.path.join(tmp, "med_dec_prev.json")
    med_dec_new = os.path.join(tmp, "med_dec_new.json")
    med_dec_out = os.path.join(tmp, "med_dec_out.json")
    jdump(med_dec_prev, {
        "pr": "org/repo#1", "last_sha": "a" * 40, "next_id": {"P": 2},
        "findings": [finding("performance", "MEDIUM", "src/p.py:10", "Slow loop", "Loop query.",
                             id="P1", status="declined", reason="Small table.")],
    })
    ch_high = finding("performance", "CRITICAL", "src/p.py:10", "Unbounded loop OOM",
                      "Loop loads 100k rows into memory.", trigger="Table has 100k rows.",
                      code_example="for row in qs.iterator(): ...", verified="confirmed",
                      challenges="P1", evidence="Production table has 100k rows.",
                      ac_ref="AC1", breaking={"consumers": "jobs", "migration": "none"})
    jdump(med_dec_new, {"last_sha": "b" * 40, "findings": [ch_high]})
    r = run("reconcile", med_dec_prev, med_dec_new, med_dec_out)
    s_ch = json.loads(r.stdout)
    f_ch = by_id(jload(med_dec_out))["P1"]
    b_ch = bmod.compute({"findings": [f_ch], "lanes": [{"lane": "performance"}]}, {"effort": "high"}, pre=[ch_high])
    check("HIGH/CRITICAL challenge to MEDIUM decline reopens with full metadata and passes breaker",
          s_ch["reopened_decline"] == ["P1"] and f_ch["status"] == "todo" and f_ch["severity"] == "CRITICAL"
          and f_ch.get("code_example") == "for row in qs.iterator(): ..."
          and f_ch.get("title") == "Unbounded loop OOM" and b_ch["valid"], (f_ch, b_ch["invalid"]))

    # 3. Duplicate challenges + simultaneous unchallenged repeat do not duplicate or contradict
    dup_ch_new = os.path.join(tmp, "dup_ch_new.json")
    dup_ch_out = os.path.join(tmp, "dup_ch_out.json")
    jdump(dup_ch_new, {"last_sha": "c" * 40, "findings": [
        finding("performance", "MEDIUM", "src/p.py:10", "Slow loop", "Loop query.",
                challenges="P1", evidence="Evidence A."),
        finding("performance", "HIGH", "src/p.py:11", "Slow loop OOM", "Loop OOM.",
                trigger="100k rows", verified="confirmed", challenges="P1", evidence="Evidence B."),
        finding("performance", "HIGH", "src/p.py:10", "Slow loop", "Loop query.",
                trigger="100k rows", verified="confirmed"),
    ]})
    r = run("reconcile", med_dec_prev, dup_ch_new, dup_ch_out)
    s_dc = json.loads(r.stdout)
    f_dc = by_id(jload(dup_ch_out))["P1"]
    check("duplicate challenges + repeat dedupe challenged list and avoid declined_still_present",
          s_dc["challenged"] == ["P1"] and s_dc["reopened_decline"] == ["P1"]
          and s_dc["declined_still_present"] == [] and "Evidence A." in f_dc["challenge"]
          and "Evidence B." in f_dc["challenge"], (s_dc, f_dc))

    # 4. MEDIUM decline repeated by confirmed HIGH without challenge passes breaker via declined_still_present
    med_rep_new = os.path.join(tmp, "med_rep_new.json")
    med_rep_out = os.path.join(tmp, "med_rep_out.json")
    rep_high = finding("performance", "HIGH", "src/p.py:14", "Slow query loop",
                       "Loop runs one query per row in table.", trigger="100k rows", verified="confirmed")
    jdump(med_rep_new, {"last_sha": "c" * 40, "findings": [rep_high]})
    run("reconcile", med_dec_prev, med_rep_new, med_rep_out)
    med_rep_doc = jload(med_rep_out)
    b_mr = bmod.compute({"findings": med_rep_doc["findings"], "lanes": [{"lane": "performance"}]},
                        {"effort": "high"}, pre=[rep_high])
    check("standing MEDIUM decline repeated by confirmed HIGH passes breaker and blocks APPROVE",
          med_rep_doc["findings"][0].get("declined_still_present") is True
          and b_mr["valid"] and b_mr["declined_blocking"] == 1 and b_mr["verdict"] == "APPROVE WITH COMMENTS",
          (med_rep_doc["findings"], b_mr))

    # 5. Explicit id matching for re-raised open.json item even when shifted >15 lines and reworded
    exp_prev = os.path.join(tmp, "exp_prev.json")
    exp_new = os.path.join(tmp, "exp_new.json")
    exp_out = os.path.join(tmp, "exp_out.json")
    jdump(exp_prev, {
        "pr": "org/repo#2", "last_sha": "a" * 40, "next_id": {"B": 3},
        "findings": [finding("adherence", "HIGH", "src/a.py:10", "Missing idempotency key",
                             "Retries create duplicate charges.", trigger="Retry POST", status="todo", id="B2")],
    })
    jdump(exp_new, {"last_sha": "b" * 40, "findings": [
        finding("adherence", "HIGH", "src/a.py:90", "Retry duplicates payment",
                "Repeated network timeouts double-bill accounts.", trigger="Retry POST", id="B2"),
    ]})
    r = run("reconcile", exp_prev, exp_new, exp_out)
    s_exp = json.loads(r.stdout)
    check("explicit stable id in new finding matches todo item across line shift and rewording",
          s_exp["kept"] == ["B2"] and s_exp["new"] == [] and s_exp["carried_open"] == [], s_exp)

    # 6. JSON null fields, HUNK_LOC, space paths, lanes_of, legacy title-only match, and PII redaction
    null_prev = os.path.join(tmp, "null_prev.json")
    null_dir = os.path.join(tmp, "null_prep")
    os.mkdir(null_dir)
    jdump(null_prev, {
        "pr": "org/repo#3", "last_sha": "a" * 40,
        "findings": [
            {"id": "X1", "lane": "critical-risk", "severity": "HIGH", "location": "my dir/a.py:10",
             "title": "Token leak for @alice_dev", "issue": "Logs secret per @org/security-team and @john.doe.",
             "trigger": "Call endpoint", "status": None, "sources": ["checklist:security"]},
            {"id": "S1", "lane": "security", "severity": "HIGH", "location": "src/s.py:5",
             "title": "Auth bypass by @dependabot[bot]", "issue": "Skips auth.", "trigger": "No header",
             "status": "declined", "reason": None},
        ],
    })
    r = run("prepare", null_prev, null_dir)
    s_np = json.loads(r.stdout)
    np_open = jload(os.path.join(null_dir, "open.json"))
    np_dec = jload(os.path.join(null_dir, "declines.json"))
    check("prepare handles status:null, reason:null, critical-risk+checklist lanes_of, and _/./team/bot handles",
          s_np["todo"] == 1 and len(s_np["warnings"]) == 1
          and np_dec[0]["reason"] == "(no reason recorded)"
          and set(np_open[0]["lanes"]) == {"critical-risk", "security"}
          and "@alice_dev" not in np_open[0]["issue"] and "@org/security-team" not in np_open[0]["issue"]
          and "@john.doe" not in np_open[0]["issue"] and "<handle>" in np_open[0]["issue"],
          (s_np, np_open, np_dec))

    null_new = os.path.join(tmp, "null_new.json")
    null_out = os.path.join(tmp, "null_out.json")
    jdump(null_new, {"last_sha": "b" * 40, "findings": [
        finding("security", "HIGH", "my dir/a.py @@ -8,3 +12,4 @@", "Token leak for @alice_dev",
                "Logs secret per @alice_dev and owner@example.com.", trigger="Call endpoint",
                code_example="@app.route('/x')\ndef f(): pass"),
        finding("security", "MEDIUM", "src/s.py:5", "Challenged", "Issue",
                challenges="S1", evidence="Proof by @john.doe", trigger=None),
    ], "cleared": [{"id": "X1", "evidence": None}]})
    r = run("reconcile", os.path.join(null_dir, "previous.json"), null_new, null_out)
    s_no = json.loads(r.stdout)
    d_no = jload(null_out)
    f_no = by_id(d_no)
    check("reconcile matches HUNK_LOC+space path, ignores null evidence clear, omits Trigger: None, redacts durable fields",
          s_no["kept"] == ["X1"] and s_no["cleared"] == [] and d_no["cleared"] == []
          and f_no["S1"]["challenge"] == "Proof by <handle>"
          and "@alice_dev" not in json.dumps(d_no) and "owner@example.com" not in json.dumps(d_no)
          and "@app.route('/x')" in f_no["X1"]["code_example"],
          (s_no, d_no))

    # Legacy title-only matching against round-2 finding with long issue + legacy->reconcile->breaker
    leg_new = os.path.join(tmp, "leg_new.json")
    leg_out = os.path.join(tmp, "leg_out.json")
    jdump(leg_new, {"last_sha": "b" * 40, "findings": [
        finding("security", "MEDIUM", "app/pay/hooks.py:9", "Webhook body not size limited",
                "The webhook handler buffers the entire incoming request stream in memory without enforcing any content length limit or reading bounds."),
    ]})
    r = run("reconcile", os.path.join(lt, "previous.json"), leg_new, leg_out)
    s_leg = json.loads(r.stdout)
    d_leg = jload(leg_out)
    b_leg = bmod.compute(
        {"findings": d_leg["findings"], "lanes": [{"lane": "security"}, {"lane": "adherence"}]},
        {"effort": "high"},
        previous=jload(os.path.join(lt, "previous.json"))["findings"],
    )
    check("legacy title-only finding matches round-2 finding with long issue and passes breaker",
          s_leg["kept"] == ["S1"] and s_leg["new"] == [] and s_leg["carried_open"] == ["X1"]
          and by_id(d_leg)["X1"].get("legacy") is True and b_leg["valid"],
          (s_leg, b_leg["invalid"]))

    # 7. level=CODEBASE carry + --help + --level CLI acceptance
    cb_prev = os.path.join(tmp, "cb_prev.json")
    cb_new = os.path.join(tmp, "cb_new.json")
    cb_out = os.path.join(tmp, "cb_out.json")
    cb_rep = os.path.join(tmp, "cb_rep.json")
    jdump(cb_prev, {
        "pr": "octo/demo#codebase", "last_sha": "1" * 40, "next_id": {"B": 2},
        "findings": [finding("adherence", "HIGH", "Codebase architecture", "Layering leak",
                             "Controllers import ORM models directly.", trigger="Call route",
                             id="B1", status="todo", introduced_by_pr="codebase")],
    })
    jdump(cb_new, {
        "pr": "octo/demo#codebase", "last_sha": "2" * 40,
        "findings": [finding("adherence", "HIGH", "Codebase architecture", "Layering leak",
                             "Controllers import ORM models directly across modules.",
                             trigger="Call route", introduced_by_pr="codebase", locations=["api/a.py:5", "api/b.py:8"])],
    })
    r = run("reconcile", cb_prev, cb_new, cb_out, "--reported", cb_rep, "--level", "CODEBASE")
    s_cb = json.loads(r.stdout)
    d_cb = jload(cb_out)
    check("CODEBASE carry preserves #codebase and matches Codebase architecture",
          s_cb["kept"] == ["B1"] and d_cb["pr"] == "octo/demo#codebase", (s_cb, d_cb))
    r_help = run("--help")
    check("--help exits 0 with usage", "Usage:" in r_help.stdout, r_help.stdout)

    # 8. mark cleanup on status reversal, null rejection, duplicate decision rejection
    rev_file = os.path.join(tmp, "rev.json")
    rev_dec = os.path.join(tmp, "rev_dec.json")
    jdump(rev_file, {
        "pr": "org/repo#4", "last_sha": "a" * 40, "next_id": {"S": 2},
        "findings": [finding("security", "MEDIUM", "a.py:1", "Title", "Issue",
                             id="S1", status="declined", reason="Old reason.", challenge="Old challenge.")],
    })
    jdump(rev_dec, [{"id": "S1", "status": "todo"}])
    run("mark", rev_file, rev_dec)
    rev_f = by_id(jload(rev_file))["S1"]
    check("mark reversing declined->todo strips stale reason/challenge and sets display_id",
          rev_f["status"] == "todo" and "reason" not in rev_f and "challenge" not in rev_f
          and rev_f.get("display_id") == "M1", rev_f)
    for bad_dec in (
        [{"id": "S1", "status": "declined", "reason": None}],
        [{"id": "S1", "status": "fixed", "commit": None}],
        [{"id": "S1", "status": "todo"}, {"id": "S1", "status": "fixed", "commit": "abc"}],
    ):
        jdump(rev_dec, bad_dec)
        p_bad = run("mark", rev_file, rev_dec, expect=2)
        check(f"mark rejects bad decision {bad_dec!r}", p_bad.returncode == 2, p_bad.stderr)
    for bad_name, bad_payload in (
        ("bad next_id letter", {"next_id": {"C": 2}, "findings": []}),
        ("next_id list", {"next_id": [], "findings": []}),
        ("cleared dict", {"findings": [], "cleared": {}}),
        ("still_open dict", {"findings": [], "still_open": {}}),
    ):
        bp = os.path.join(tmp, "bp.json")
        jdump(bp, bad_payload)
        cmd = ("prepare", bp, tmp) if "next_id" in bad_name else ("reconcile", prev1, bp, os.path.join(tmp, "bo.json"))
        p_b = run(*cmd, expect=2)
        check(f"bad input: {bad_name} rejected cleanly", p_b.returncode == 2, p_b.stderr)

print()
if FAILS:
    print("FAILED:", len(FAILS))
    sys.exit(1)
print("ALL PASS")
