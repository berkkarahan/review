#!/usr/bin/env python3
"""Deterministic gates G1 (schema), G2 (in-diff), G3 (trigger), G4 (confidence).

Usage: schema_lint.py <findings.json> <diff-map.json>
Input: a JSON array of Finding objects (the lead converts each lane's YAML block).
Output: JSON {"results": [{"id", "action", "reasons", "kept_locations"?}], "summary": {...}} on stdout.
Exit code: 0 always when the input parses. The lead acts on `action`:
  keep       - finding passes all gates
  retry_lane - G1 failed: schema fields missing or invalid. Ask the lane once.
  drop       - G2 failed (outside PR diff) or G4 failed (LOW confidence on MEDIUM/LOW).
  downgrade  - G3 failed: no trigger on CRITICAL/HIGH. Cap at MEDIUM, or QUESTION.
Optional fields (absent = no check): status todo|fixed|declined, origin new|existing,
eval_disposition post|chat-only|drop, eval_downgraded bool, and the strings id,
reopened_from, challenges, reason, commit, eval_reason. A bad value is a G1 failure.
Stdlib only.
"""
import json
import re
import sys

SEV = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "QUESTION"}
CONF = {"HIGH", "MEDIUM", "LOW"}
LANES = {"adherence", "business-logic", "code-quality", "security", "performance",
         "compatibility", "robustness", "conventions", "skim", "critical-risk", "evaluation"}
INTENT_LANES = {"adherence", "business-logic", "critical-risk", "skim"}
OPT_ENUM = {
    "status": {"todo", "fixed", "declined"},
    "origin": {"new", "existing"},
    "eval_disposition": {"post", "chat-only", "drop"},
}
OPT_TEXT = ("reopened_from", "challenges", "reason", "commit", "eval_reason")
LOC = re.compile(r"^(?P<path>[^\s:]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
HUNK_LOC = re.compile(r"^(?P<path>[^\s:]+)\s+@@\s+-\d+(?:,\d+)?\s+\+(?P<a>\d+)(?:,(?P<count>\d+))?\s+@@")
INTENT_LOC = re.compile(r"^(PR description|intent\.md\s+§.+)$")
NEAR = 0  # a finding must sit on an added line. Raise to allow context lines nearby.


def norm_path(p):
    for prefix in ("head/",):
        while p.startswith(prefix):
            p = p[len(prefix):]
    return p


def on_changed_line(loc, dmap):
    s = loc.strip()
    m = LOC.match(s)
    if m:
        path = norm_path(m.group("path"))
        a = int(m.group("a"))
        b = int(m.group("b") or a)
    else:
        hm = HUNK_LOC.match(s)
        if not hm:
            return None  # invalid or non-line location format
        path = norm_path(hm.group("path"))
        a = int(hm.group("a"))
        count = int(hm.group("count") or 1)
        b = a + count - 1
    entry = dmap.get(path)
    if entry is None:
        return False
    added = set(entry["added"])
    return any(n in added or any(abs(n - x) <= NEAR for x in added) for n in range(a, b + 1))


def check_detail(f, dmap):
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
    if f.get("locations") is not None and not isinstance(f.get("locations"), list):
        reasons.append("G1 invalid locations")
    for key, allowed in OPT_ENUM.items():
        if f.get(key) is not None and not (isinstance(f[key], str) and f[key] in allowed):
            reasons.append(f"G1 invalid {key}")
    for key in OPT_TEXT:
        if f.get(key) is not None and not (isinstance(f[key], str) and f[key].strip()):
            reasons.append(f"G1 invalid {key}")
    if f.get("eval_downgraded") is not None and not isinstance(f["eval_downgraded"], bool):
        reasons.append("G1 invalid eval_downgraded")
    if f.get("introduced_by_pr") == "pre-existing" and not reasons:
        return "drop", ["G2 pre-existing finding outside PR diff"], None
    if reasons:
        return "retry_lane", reasons, None

    loc = str(f["location"]).strip()
    kept_locs = None
    if INTENT_LOC.match(loc):
        if f.get("lane") not in INTENT_LANES:
            return "drop", [f"G2 intent-only location not allowed for lane {f.get('lane')}: {loc}"], None
    else:
        extra = f.get("locations") or []
        all_locs = [loc] + [str(x).strip() for x in extra]
        verdicts = [on_changed_line(x, dmap) for x in all_locs]
        if verdicts[0] is None and not any(v is True for v in verdicts):
            return "retry_lane", ["G1 invalid location format (expected path:line): " + loc], None
        filtered = [x for x, v in zip(all_locs, verdicts) if v is True]
        if not filtered:
            return "drop", ["G2 location is outside the PR diff: " + loc], None
        if len(filtered) < len(all_locs):
            kept_locs = filtered

    sev, conf = f["severity"], f["confidence"]
    trig = str(f.get("trigger", "")).strip()
    if sev in ("CRITICAL", "HIGH") and (not trig or trig.lower().startswith("n/a")):
        if f.get("lane") == "conventions" and trig.lower().startswith("n/a (convention)"):
            pass
        else:
            return "downgrade", ["G3 no concrete trigger for " + sev], kept_locs
    if sev in ("MEDIUM", "LOW") and conf == "LOW":
        return "drop", ["G4 LOW confidence on a " + sev + " finding"], None
    if conf == "LOW" and sev not in ("QUESTION", "CRITICAL", "HIGH"):
        return "drop", ["G4 LOW confidence"], None
    return "keep", [], kept_locs


def check(f, dmap):
    action, reasons, _kept = check_detail(f, dmap)
    return action, reasons


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: schema_lint.py <findings.json> <diff-map.json>")
    findings = json.load(open(sys.argv[1], encoding="utf-8"))
    dmap = json.load(open(sys.argv[2], encoding="utf-8"))
    results, summary = [], {}
    for f in findings:
        action, reasons, kept_locs = check_detail(f, dmap)
        summary[action] = summary.get(action, 0) + 1
        row = {"id": f.get("id"), "action": action, "reasons": reasons}
        if kept_locs is not None and action in ("keep", "downgrade"):
            row["kept_locations"] = kept_locs
        results.append(row)
    json.dump({"results": results, "summary": summary}, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
