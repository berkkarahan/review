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
                              finding("conventions", "QUESTION", "PR description", "Which clients?", "The text does not say which clients read it.")]}
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

print()
if FAILS:
    print("FAILED:", len(FAILS))
    sys.exit(1)
print("ALL PASS")
