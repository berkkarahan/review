#!/usr/bin/env python3
"""Deterministic gates G1 (schema), G2 (in-diff), G3 (trigger), G4 (confidence).

Usage: schema_lint.py <findings.json> <diff-map.json>
Input: a JSON array of Finding objects (the lead converts each lane's YAML block).
Output: JSON {"results": [{"id", "action", "reasons"}], "summary": {...}} on stdout.
Exit code: 0 always when the input parses. The lead acts on `action`:
  keep             - finding passes all gates
  retry_lane       - G1 failed: schema fields missing or invalid. Ask the lane once.
  move_preexisting - G2 failed: location is not on a changed line. Chat-only.
  downgrade        - G3 failed: no trigger on CRITICAL/HIGH. Cap at MEDIUM, or QUESTION.
  drop             - G4 failed: MEDIUM/LOW severity with LOW confidence.
Optional fields (absent = no check): status todo|fixed|declined, origin new|existing,
and the strings id, reopened_from, challenges, reason, commit. A bad value is a G1 failure.
Stdlib only.
"""
import json
import re
import sys

SEV = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "QUESTION"}
CONF = {"HIGH", "MEDIUM", "LOW"}
LANES = {"adherence", "business-logic", "code-quality", "security", "performance",
         "compatibility", "robustness", "conventions", "skim"}
OPT_ENUM = {"status": {"todo", "fixed", "declined"}, "origin": {"new", "existing"}}
OPT_TEXT = ("reopened_from", "challenges", "reason", "commit")
LOC = re.compile(r"^(?P<path>[^\s:]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
INTENT_LOC = re.compile(r"^(PR description|intent\.md\s+§.+)$")
NEAR = 0  # a finding must sit on an added line. Raise to allow context lines nearby.


def norm_path(p):
    for prefix in ("head/",):
        while p.startswith(prefix):
            p = p[len(prefix):]
    return p


def on_changed_line(loc, dmap):
    m = LOC.match(loc.strip())
    if not m:
        return None  # not a path:line (hunk header or intent-only): cannot judge
    path = norm_path(m.group("path"))
    a = int(m.group("a"))
    b = int(m.group("b") or a)
    entry = dmap.get(path)
    if entry is None:
        return False
    added = set(entry["added"])
    return any(n in added or any(abs(n - x) <= NEAR for x in added) for n in range(a, b + 1))


def check(f, dmap):
    reasons = []
    for key in ("id", "lane", "severity", "confidence", "title", "location", "issue", "fix"):
        if not str(f.get(key, "")).strip():
            reasons.append(f"G1 missing field: {key}")
    if f.get("severity") not in SEV:
        reasons.append("G1 invalid severity")
    if f.get("confidence") not in CONF:
        reasons.append("G1 invalid confidence")
    if f.get("lane") not in LANES:
        reasons.append("G1 invalid lane")
    if len(str(f.get("title", ""))) > 40:
        reasons.append("G1 title longer than 40 characters")
    if f.get("severity") == "CRITICAL" and not str(f.get("code_example", "")).strip():
        reasons.append("G1 CRITICAL needs code_example")
    if f.get("introduced_by_pr") == "pre-existing":
        reasons.append("G1 pre-existing finding in FINDINGS")
    for key, allowed in OPT_ENUM.items():
        if f.get(key) is not None and not (isinstance(f[key], str) and f[key] in allowed):
            reasons.append(f"G1 invalid {key}")
    for key in OPT_TEXT:
        if f.get(key) is not None and not (isinstance(f[key], str) and f[key].strip()):
            reasons.append(f"G1 invalid {key}")
    if any(r.startswith("G1") for r in reasons):
        if f.get("introduced_by_pr") == "pre-existing" and len(reasons) == 1:
            return "move_preexisting", reasons
        return "retry_lane", reasons

    loc = str(f["location"])
    if not INTENT_LOC.match(loc):
        verdict = on_changed_line(loc, dmap)
        locs = [loc] + [str(x) for x in f.get("locations", [])]
        flags = [on_changed_line(x, dmap) for x in locs]
        if verdict is False and not any(flags):
            return "move_preexisting", ["G2 location is not on a changed line: " + loc]

    sev, conf = f["severity"], f["confidence"]
    trig = str(f.get("trigger", "")).strip()
    if sev in ("CRITICAL", "HIGH") and (not trig or trig.lower().startswith("n/a")):
        if f.get("lane") == "conventions" and trig.lower().startswith("n/a (convention)"):
            pass
        else:
            return "downgrade", ["G3 no concrete trigger for " + sev]
    if sev in ("MEDIUM", "LOW") and conf == "LOW":
        return "drop", ["G4 LOW confidence on a " + sev + " finding"]
    if conf == "LOW" and sev not in ("QUESTION", "CRITICAL", "HIGH"):
        return "drop", ["G4 LOW confidence"]
    return "keep", []


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: schema_lint.py <findings.json> <diff-map.json>")
    findings = json.load(open(sys.argv[1], encoding="utf-8"))
    dmap = json.load(open(sys.argv[2], encoding="utf-8"))
    results, summary = [], {}
    for f in findings:
        action, reasons = check(f, dmap)
        summary[action] = summary.get(action, 0) + 1
        results.append({"id": f.get("id"), "action": action, "reasons": reasons})
    json.dump({"results": results, "summary": summary}, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
