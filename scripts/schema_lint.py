#!/usr/bin/env python3
"""Deterministic gates G1 (schema), G2 (in-diff / codebase file-map), G3 (trigger), G4 (confidence).

Usage: schema_lint.py <findings.json> <diff-or-file-map.json> [--level PR|CODEBASE]
Input: a JSON array of Finding objects, or {"findings": [...]} (compact JSON from Phase 1 lanes).
Output: JSON {"results": [{"id", "action", "reasons", "kept_locations"?}], "summary": {...}} on stdout.
Exit code: 0 always when the input parses; 2 on usage, unreadable file, or malformed JSON.
The lead acts on `action`:
  keep       - finding passes all gates
  retry_lane - G1 failed: schema fields missing or invalid. Ask the lane once.
  drop       - G2 failed (outside PR diff or invalid codebase file/line) or G4 failed (LOW confidence on MEDIUM/LOW).
  downgrade  - G3 failed: no trigger on CRITICAL/HIGH. Cap at MEDIUM, or QUESTION.
Optional fields (absent = no check): status todo|fixed|declined, origin new|existing,
eval_disposition post|chat-only|drop, eval_downgraded bool, and the strings id,
reopened_from, challenges, reason, commit, eval_reason. A bad value is a G1 failure.
Stdlib only.
"""
import argparse
import json
import re
import sys

REVIEW_LEVELS = ("PR", "CODEBASE")
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
LOC = re.compile(r"^(?P<path>[^\s:\r\n](?:[^:\r\n]*[^\s:\r\n])?):(?P<a>\d+)(?:-(?P<b>\d+))?$")
HUNK_LOC = re.compile(
    r"^(?P<path>[^\s:\r\n](?:[^:\r\n]*[^\s:\r\n])?)\s+@@\s+-\d+(?:,\d+)?\s+\+(?P<a>\d+)(?:,(?P<count>\d+))?\s+@@(?:\s.*)?$"
)
PR_INTENT_LOC = re.compile(r"^(PR description|intent\.md\s+§.+)$")
CODEBASE_INTENT_LOC = re.compile(r"^(Codebase architecture(?:\s+§.+)?|intent\.md\s+§.+)$")
INTENT_LOC = re.compile(r"^(PR description|intent\.md\s+§.+|Codebase architecture(?:\s+§.+)?)$")
NEAR = 0  # a finding must sit on an added line. Raise to allow context lines nearby.


def norm_path(p):
    p = str(p or "").strip()
    while True:
        if p.startswith("./"):
            p = p[2:]
        elif p.startswith("head/"):
            p = p[5:]
        else:
            break
    return p


def on_changed_line(loc, dmap):
    if not isinstance(dmap, dict):
        return False
    s = str(loc or "").strip()
    m = LOC.match(s)
    if m:
        path = norm_path(m.group("path"))
        a = int(m.group("a"))
        b = int(m.group("b") or a)
        if a < 1 or b < a:
            return False
        entry = dmap.get(path)
        if not isinstance(entry, dict) or not isinstance(entry.get("added"), list):
            return False
        return any(
            a - NEAR <= x <= b + NEAR
            for x in entry["added"]
            if isinstance(x, int) and not isinstance(x, bool)
        )

    hm = HUNK_LOC.match(s)
    if not hm:
        return None  # invalid or non-line location format
    path = norm_path(hm.group("path"))
    a = int(hm.group("a"))
    count_raw = hm.group("count")
    count = 1 if count_raw is None else int(count_raw)
    entry = dmap.get(path)
    if not isinstance(entry, dict) or not isinstance(entry.get("added"), list):
        return False
    if count == 0:
        # Pure-deletion hunk (+a,0): check anchor line against context and added lines.
        anchor = max(1, a)
        added = entry.get("added") or []
        context = entry.get("context") or []
        return (
            any(abs(anchor - x) <= NEAR for x in added if isinstance(x, int) and not isinstance(x, bool))
            or any(abs(anchor - x) <= NEAR for x in context if isinstance(x, int) and not isinstance(x, bool))
        )
    if a < 1:
        return False
    b = a + count - 1
    return any(
        a - NEAR <= x <= b + NEAR
        for x in entry["added"]
        if isinstance(x, int) and not isinstance(x, bool)
    )


def on_valid_file_line(loc, file_map):
    """Validate path:line[-end] against codebase file-map.json ({"path": {"lines": N}})."""
    if not isinstance(file_map, dict):
        return False
    s = str(loc or "").strip()
    m = LOC.match(s)
    if not m:
        return None
    path = norm_path(m.group("path"))
    a = int(m.group("a"))
    b = int(m.group("b") or a)
    if a < 1 or b < a:
        return False
    entry = file_map.get(path)
    if entry is None:
        return False
    total = entry.get("lines") if isinstance(entry, dict) else (entry if isinstance(entry, int) and not isinstance(entry, bool) else None)
    if total is None or isinstance(total, bool):
        return False
    return b <= max(1, int(total))


def check_detail(f, dmap, level="PR"):
    if not isinstance(f, dict):
        return "retry_lane", ["G1 finding is not an object"], None
    level = str(level or "PR").upper()
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
    if f.get("locations") is not None:
        locs_val = f.get("locations")
        if not isinstance(locs_val, list) or not all(isinstance(x, str) and x.strip() for x in locs_val):
            reasons.append("G1 invalid locations")
    for key, allowed in OPT_ENUM.items():
        if f.get(key) is not None and not (isinstance(f[key], str) and f[key] in allowed):
            reasons.append(f"G1 invalid {key}")
    for key in OPT_TEXT:
        if f.get(key) is not None and not (isinstance(f[key], str) and f[key].strip()):
            reasons.append(f"G1 invalid {key}")
    if f.get("eval_downgraded") is not None and not isinstance(f["eval_downgraded"], bool):
        reasons.append("G1 invalid eval_downgraded")
    if level == "PR" and f.get("introduced_by_pr") == "pre-existing" and not reasons:
        return "drop", ["G2 pre-existing finding outside PR diff"], None
    if reasons:
        return "retry_lane", reasons, None

    loc = str(f["location"]).strip()
    extra = [str(x).strip() for x in (f.get("locations") or [])]
    loc_fn = on_valid_file_line if level == "CODEBASE" else on_changed_line
    intent_rx = CODEBASE_INTENT_LOC if level == "CODEBASE" else PR_INTENT_LOC
    kept_locs = None

    if intent_rx.match(loc):
        if f.get("lane") not in INTENT_LANES:
            return "drop", [f"G2 intent-only location not allowed for lane {f.get('lane')}: {loc}"], None
        if extra:
            verdicts = [True if intent_rx.match(x) else loc_fn(x, dmap) for x in extra]
            filtered = [x for x, v in zip(extra, verdicts) if v is True]
            if len(filtered) < len(extra):
                kept_locs = [loc] + filtered
    elif INTENT_LOC.match(loc):
        return "drop", [f"G2 intent location not valid at {level} level: {loc}"], None
    else:
        all_locs = [loc] + extra
        verdicts = [loc_fn(x, dmap) for x in all_locs]
        if verdicts[0] is None and not any(v is True for v in verdicts):
            return "retry_lane", ["G1 invalid location format (expected path:line): " + loc], None
        filtered = [x for x, v in zip(all_locs, verdicts) if v is True]
        if not filtered:
            if level == "CODEBASE":
                return "drop", ["G2 location is outside the codebase scope or line range: " + loc], None
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
    return "keep", [], kept_locs


def check(f, dmap, level="PR"):
    action, reasons, _kept = check_detail(f, dmap, level=level)
    return action, reasons


def main(argv=None):
    ap = argparse.ArgumentParser(prog="schema_lint.py", description=__doc__.splitlines()[0])
    ap.add_argument("findings")
    ap.add_argument("loc_map")
    ap.add_argument("--level", type=str.upper, choices=REVIEW_LEVELS, default="PR")
    args = ap.parse_args(argv)
    try:
        with open(args.findings, encoding="utf-8") as fh:
            findings = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"schema_lint: cannot read findings {args.findings}: {exc}\n")
        return 2
    try:
        with open(args.loc_map, encoding="utf-8") as fh:
            dmap = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"schema_lint: cannot read location map {args.loc_map}: {exc}\n")
        return 2

    if isinstance(findings, dict) and isinstance(findings.get("findings"), list):
        findings = findings["findings"]
    if not isinstance(findings, list):
        sys.stderr.write("schema_lint: findings must be a JSON array or an object with a findings list\n")
        return 2
    if not isinstance(dmap, dict):
        sys.stderr.write("schema_lint: location map must be a JSON object\n")
        return 2

    results, summary = [], {}
    for f in findings:
        action, reasons, kept_locs = check_detail(f, dmap, level=args.level)
        summary[action] = summary.get(action, 0) + 1
        fid = f.get("id") if isinstance(f, dict) else None
        row = {"id": fid, "action": action, "reasons": reasons}
        if kept_locs is not None and action in ("keep", "downgrade"):
            row["kept_locations"] = kept_locs
        results.append(row)
    json.dump({"results": results, "summary": summary}, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
