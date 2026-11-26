#!/usr/bin/env python3
"""Deterministic breaker: open counts, completeness, quality-gate band, verdict and event.

Usage:
  breaker.py <findings.json> [--pre <pre.json>] [--previous <previous.json>]
             [--level PR|CODEBASE] [--effort high|medium|low|skim]
             [--hold-event REQUEST_CHANGES|COMMENT] [--author-is-me]
             [--in-profile] [--ci <value>]

Input (<findings.json>): a JSON object, or a bare JSON array of findings:
  {"findings": [Finding...],          merged and verified set (SKILL.md Phase 2.2)
   "ac_matrix": [ACRow...],           statuses after verification
   "lanes": [{"lane", "status"?, "reason"?, "high_risk"?, "cleared"?}...],
   "folded": ["security", ...],       medium/high: folded lanes
   "level", "effort", "hold_event", "author_is_me", "in_profile", "ci"  optional; CLI flags override}
A lane status is returned (default), abstained, or NOT ASSESSED (alias: failed).
An abstained lane counts as returned.

Findings that do not count: verified "refuted", eval_disposition "drop"/"chat-only",
origin "existing", and (at level=PR) introduced_by_pr "pre-existing". A finding with
no status counts as todo, and one with no origin counts as new.

Output: JSON on stdout:
  {"valid", "level", "effort", "open", "unsatisfied", "declined_blocking", "complete",
   "band", "verdict", "emoji", "phrase", "event", "next_step", "reasons", "notes", "invalid"}
Exit code: 0 when valid. 1 when invalid, with one "INVALID: ..." line on stderr for
each problem. 2 for a usage error. The script never writes a file and never changes
the findings. The verdict and the event come from this script only. Do not recompute them.
Stdlib only.

Verdict rules (the code below is the authority). Effort never changes the verdict, the event
or the next step. Effort sets only how deep the review looked (reference/effort/levels.md).
  Checked in order, at every effort:
    any open CRITICAL, any Missing/Contradicted AC, any breaking change with no
    migration, or any open HIGH         -> REQUEST CHANGES
    any MEDIUM or QUESTION              -> APPROVE WITH COMMENTS
    only LOW, or nothing                -> APPROVE
  A CRITICAL or HIGH that is plausible or unverified counts the same as a confirmed one.
  Event at level=PR: REQUEST CHANGES -> voice.hold_event (default REQUEST_CHANGES),
    APPROVE WITH COMMENTS -> COMMENT, APPROVE -> APPROVE. author_is_me makes it COMMENT.
  --in-profile and --ci are accepted for compatibility. They have no effect on the verdict or event.
  skim (PR only; rejected at CODEBASE): a NOT ASSESSED skim lane sets valid=False (no verdict).
  Every level: a NOT ASSESSED lane caps APPROVE at APPROVE WITH COMMENTS (the reason
    names the lane). A new CRITICAL or HIGH with status declined (or a standing confirmed
    repeat decline) counts in declined_blocking and blocks APPROVE.
  CODEBASE (level=CODEBASE): maps REQUEST CHANGES -> NEEDS ACTION, APPROVE WITH COMMENTS ->
    IMPROVEMENTS RECOMMENDED, APPROVE -> HEALTHY; counts pre-existing findings; event is
    always NONE (even when author_is_me is true).
  next_step (from the PR-level verdict, before the CODEBASE mapping):
    fix       REQUEST CHANGES: open blocking items remain.
    rerun     a lane is NOT ASSESSED. Run the review again.
    document  APPROVE WITH COMMENTS: only non-blocking or declined items remain. Fix them or
              record a decline reason (carry_findings.py mark), then stop.
    done      nothing is left to do.
    null      the input is invalid.
  Quality gate (informational, never changes the verdict; high and medium): band code-quality, performance and security (via lane or
    sources); CRITICAL or HIGH -> FAIL, MEDIUM -> CONCERNS, LOW or none -> PASS; the worst
    band wins. A NOT ASSESSED lane sets complete false and caps the band at CONCERNS. Medium
    adds "(partial)" when a lane was folded. Low and skim: band n/a.
"""
import argparse
import json
import re
import sys

LEVELS = ("high", "medium", "low", "skim")
REVIEW_LEVELS = ("PR", "CODEBASE")
SEVS = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "QUESTION")
RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "QUESTION": 0}
AC_STATUS = {"Met", "Partial", "Missing", "Contradicted", "Not verifiable"}
FINDING_STATUS = {"todo", "fixed", "declined"}
ORIGINS = {"new", "existing"}
LANE_NAMES = {"adherence", "business-logic", "code-quality", "security", "performance",
              "compatibility", "robustness", "conventions", "skim", "critical-risk", "evaluation"}
LANE_STATUS = {"returned", "abstained", "NOT ASSESSED", "failed"}
NOT_ASSESSED = {"NOT ASSESSED", "failed"}
EVAL_DISPOSITIONS = {"post", "chat-only", "drop"}
BAND_LANES = ("code-quality", "performance", "security")
HOLD_EVENTS = ("REQUEST_CHANGES", "COMMENT")
FLOOR_KEYWORDS = (
    "data loss", "data-loss", "data corruption", "data-corruption",
    "corrupt", "injection", "auth", "secret", "secrets", "credential",
)

# Set False so any open QUESTION yields APPROVE WITH COMMENTS (matches verdict.md & skim table).
QUESTION_ONLY_APPROVES = False

LOC_SITE_RX = re.compile(r"^(?P<p>[^\n:]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
HUNK_SITE_RX = re.compile(r"^(?P<p>.+?)\s+@@\s+-\d+(?:,\d+)?\s+\+(?P<a>\d+)(?:,(?P<count>\d+))?\s+@@")
NO_MIGRATION = re.compile(
    r"^\s*(?:no|nil|none\b.*|n/a\b.*|na|tbd|unknown|no migration\b.*|not provided\b.*)?\s*\.?\s*$",
    re.I)

PHRASE = {
    "REQUEST CHANGES": ("🔴", "Not ready to merge", "Not ready for review"),
    "APPROVE WITH COMMENTS": ("🟡", "Ready to merge after the comments", "Ready for review"),
    "APPROVE": ("🟢", "Ready to merge", "Ready for review"),
}
CODEBASE_VERDICT_MAP = {
    "REQUEST CHANGES": "NEEDS ACTION",
    "APPROVE WITH COMMENTS": "IMPROVEMENTS RECOMMENDED",
    "APPROVE": "HEALTHY",
}
CODEBASE_PHRASE = {
    "NEEDS ACTION": ("🔴", "Codebase needs urgent fixes"),
    "IMPROVEMENTS RECOMMENDED": ("🟡", "Codebase needs targeted improvements"),
    "HEALTHY": ("🟢", "Codebase is in good shape"),
}


def present(v):
    return v is not None and not isinstance(v, (list, dict, bool)) and str(v).strip() != ""


def is_str_present(v):
    return isinstance(v, str) and v.strip() != ""


def sev_of(f):
    return f.get("severity")


def status_of(f):
    return f.get("status") or "todo"


def origin_of(f):
    return f.get("origin") or "new"


def norm_path(p):
    p = str(p or "").strip()
    changed = True
    while changed:
        changed = False
        for prefix in ("head/", "./"):
            if p.startswith(prefix):
                p = p[len(prefix):]
                changed = True
    return p


def excluded(f, level="PR"):
    if f.get("verified") == "refuted" or f.get("eval_disposition") in ("drop", "chat-only"):
        return True
    if level == "PR" and f.get("introduced_by_pr") == "pre-existing":
        return True
    return False


def tokens(text):
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return {t.upper() for t in re.split(r"[\s,;/()]+", text) if t}


def has_trigger(f):
    trig = str(f.get("trigger") or "").strip()
    if trig and not trig.lower().startswith("n/a"):
        return True
    return f.get("lane") == "conventions" and trig.lower().startswith("n/a (convention)")


def breaking_without_migration(f):
    b = f.get("breaking")
    if not b:
        return False
    if not isinstance(b, dict):
        return True
    return bool(NO_MIGRATION.match(str(b.get("migration") or "")))


def findings_of(obj):
    if isinstance(obj, dict):
        return obj.get("findings") if obj.get("findings") is not None else []
    return obj if isinstance(obj, list) else []


def lane_state(entry):
    st = entry.get("status")
    if isinstance(st, str) and st in NOT_ASSESSED:
        return "NOT ASSESSED"
    return st or "returned"


def finding_lanes(f):
    lanes_set = set()
    if isinstance(f.get("lane"), str) and f["lane"]:
        lanes_set.add(f["lane"])
    srcs = f.get("sources")
    if isinstance(srcs, list):
        for s in srcs:
            if isinstance(s, str) and s.strip():
                val = s.strip()
                lanes_set.add(val.split(":", 1)[1].strip() if ":" in val else val)
    return lanes_set


def validate(findings, ac_rows, lanes, effort, invalid, level="PR"):
    """Structure and enum checks, including the optional contract fields."""
    seen = set()
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            invalid.append(f"finding #{i} is not an object")
            continue
        raw_id = f.get("id")
        fid = raw_id if isinstance(raw_id, str) and raw_id.strip() else f"#{i}"
        if raw_id is not None and not isinstance(raw_id, str):
            invalid.append(f"#{i}: id must be a string")
        elif is_str_present(raw_id):
            if raw_id in seen:
                invalid.append(f"{fid}: duplicate id")
            seen.add(raw_id)
        sev = f.get("severity")
        if not isinstance(sev, str) or sev not in SEVS:
            invalid.append(f"{fid}: severity is not one of {', '.join(SEVS)}")
        if f.get("status") is not None and (not isinstance(f["status"], str) or f["status"] not in FINDING_STATUS):
            invalid.append(f"{fid}: status is not one of todo, fixed, declined")
        if f.get("origin") is not None and (not isinstance(f["origin"], str) or f["origin"] not in ORIGINS):
            invalid.append(f"{fid}: origin is not one of new, existing")
        if f.get("legacy") is not None and not isinstance(f["legacy"], bool):
            invalid.append(f"{fid}: legacy must be a boolean")
        if f.get("status") == "fixed" and not present(f.get("commit")):
            invalid.append(f"{fid}: status fixed needs a commit")
        if f.get("status") == "declined" and not present(f.get("reason")):
            invalid.append(f"{fid}: status declined needs a reason")
        if f.get("eval_disposition") is not None and (
            not isinstance(f["eval_disposition"], str) or f["eval_disposition"] not in EVAL_DISPOSITIONS
        ):
            invalid.append(f"{fid}: eval_disposition is not one of post, chat-only, drop")
        if f.get("eval_downgraded") is not None and not isinstance(f["eval_downgraded"], bool):
            invalid.append(f"{fid}: eval_downgraded must be a boolean")
        if f.get("eval_downgraded") is True and not present(f.get("eval_reason")):
            invalid.append(f"{fid}: eval_downgraded needs eval_reason")
        if f.get("locations") is not None and (
            not isinstance(f["locations"], list) or not all(isinstance(x, str) for x in f["locations"])
        ):
            invalid.append(f"{fid}: locations must be a list of strings")
        if f.get("merged_titles") is not None and (
            not isinstance(f["merged_titles"], list) or not all(isinstance(x, str) for x in f["merged_titles"])
        ):
            invalid.append(f"{fid}: merged_titles must be a list of strings")
        if f.get("challenges") is not None and not isinstance(f["challenges"], str):
            invalid.append(f"{fid}: challenges must be a string")
        if excluded(f, level=level):
            continue
        if f.get("status") in ("fixed", "declined"):
            continue
        if isinstance(sev, str) and sev in ("CRITICAL", "HIGH"):
            if not (f.get("legacy") is True or f.get("origin") == "existing") and not has_trigger(f):
                invalid.append(f"{fid}: {sev} has no trigger")
    ids = {f["id"]: f for f in findings if isinstance(f, dict) and is_str_present(f.get("id"))}
    for f in findings:
        if isinstance(f, dict) and is_str_present(f.get("challenges")):
            target = ids.get(f["challenges"])
            if target is None or target.get("status") != "declined":
                invalid.append(f"{f.get('id') or '?'}: challenges {f['challenges']}, "
                               "which is not a declined finding in this set")
    for i, r in enumerate(ac_rows):
        if not isinstance(r, dict) or not is_str_present(r.get("ref")):
            invalid.append(f"ac_matrix row #{i} has no ref")
        elif not isinstance(r.get("status"), str) or r.get("status") not in AC_STATUS:
            invalid.append(f"{r['ref']}: AC status is not one of {', '.join(sorted(AC_STATUS))}")
    for i, ln in enumerate(lanes):
        if not isinstance(ln, dict) or not isinstance(ln.get("lane"), str) or ln.get("lane") not in LANE_NAMES:
            invalid.append(f"lane #{i} has an unknown lane name")
            continue
        if ln.get("status") is not None and (not isinstance(ln["status"], str) or ln["status"] not in LANE_STATUS):
            invalid.append(f"{ln['lane']}: lane status is not one of returned, abstained, NOT ASSESSED")
        st = lane_state(ln)
        if st in ("abstained", "NOT ASSESSED") and not present(ln.get("reason")):
            invalid.append(f"{ln['lane']}: lane status {st} needs a reason")
        cleared_list = ln.get("cleared")
        if cleared_list is not None and not isinstance(cleared_list, list):
            invalid.append(f"{ln['lane']}: cleared must be a list")
        else:
            for c in cleared_list or []:
                if not isinstance(c, dict) or not is_str_present(c.get("id")) or not present(c.get("evidence")):
                    invalid.append(f"{ln['lane']}: a cleared item needs an id and evidence")
        if ln.get("high_risk") is not None and not isinstance(ln["high_risk"], str):
            invalid.append(f"{ln['lane']}: high_risk must be a string")


def check_ac_named(findings, ac_rows, invalid, level="PR"):
    named = set()
    for f in findings:
        if isinstance(f, dict) and not excluded(f, level=level):
            named |= tokens(f.get("ac_ref"))
    for r in ac_rows:
        if isinstance(r, dict) and r.get("status") in ("Missing", "Contradicted"):
            if isinstance(r.get("ref"), str) and r["ref"].upper() not in named:
                invalid.append(f"{r['ref']}: AC is {r['status']} but no finding names it in ac_ref")


def loc_sites(f):
    out = []
    extra = f.get("locations") if isinstance(f.get("locations"), list) else []
    rep = f.get("repeat_locations") if isinstance(f.get("repeat_locations"), list) else []
    for loc in [f.get("location")] + list(extra) + list(rep):
        if not isinstance(loc, str):
            continue
        s = loc.strip()
        m = LOC_SITE_RX.match(s)
        if m:
            a = int(m.group("a"))
            b = int(m.group("b") or a)
            if a >= 1 and b >= a:
                out.append((norm_path(m.group("p")), a, b))
            continue
        hm = HUNK_SITE_RX.match(s)
        if hm:
            a = int(hm.group("a"))
            count = int(hm.group("count") or 1)
            b = a if count == 0 else a + count - 1
            out.append((norm_path(hm.group("p")), a, b))
    return out


def same_flaw(pre, post):
    if is_str_present(pre.get("id")) and pre.get("id") == post.get("id"):
        return True
    t = str(pre.get("title") or "").strip().lower() if isinstance(pre.get("title"), str) else ""
    merged = post.get("merged_titles") if isinstance(post.get("merged_titles"), list) else []
    post_title = str(post.get("title") or "").strip().lower() if isinstance(post.get("title"), str) else ""
    names = ([post_title] if post_title else []) + [str(x).strip().lower() for x in merged if isinstance(x, str)]
    if t and t in names:
        return True
    pre_loc = norm_path(pre.get("location")) if isinstance(pre.get("location"), str) else ""
    if pre_loc and isinstance(post.get("repeat_locations"), list):
        rep_norm = {norm_path(x) for x in post["repeat_locations"] if isinstance(x, str)}
        if pre_loc in rep_norm:
            return True
    for p, a, b in loc_sites(pre):
        for q, c, d in loc_sites(post):
            if p == q and a - 5 <= d and c - 5 <= b:
                return True
    return False


def is_floor_protected(pf):
    """Return True when a confirmed finding is strictly protected from evaluation drops or downgrades."""
    if not isinstance(pf, dict) or pf.get("verified") != "confirmed":
        return False
    if sev_of(pf) == "CRITICAL":
        return True
    if sev_of(pf) != "HIGH":
        return False
    if pf.get("lane") == "security" or breaking_without_migration(pf) or present(pf.get("ac_ref")):
        return True
    text = " ".join(str(pf.get(k) or "") for k in ("dimension", "impact", "title", "issue")).lower()
    return any(kw in text for kw in FLOOR_KEYWORDS)


def check_floor(pre_findings, findings, invalid):
    """Safety floor: a verified CRITICAL or HIGH of the pre-merge set stays, at the same level or higher.
    Non-floor HIGH findings may be downgraded only by evaluation with eval_downgraded=True and eval_reason.
    Standing confirmed repeats of earlier declines remain declined when flagged with declined_still_present."""
    for pf in pre_findings:
        if not isinstance(pf, dict) or pf.get("verified") != "confirmed":
            continue
        if not isinstance(sev_of(pf), str) or sev_of(pf) not in ("CRITICAL", "HIGH"):
            continue
        matches = [f for f in findings if isinstance(f, dict) and same_flaw(pf, f)]
        active = [m for m in matches if m.get("eval_disposition") not in ("drop", "chat-only")]
        label = f"{pf.get('id') or '?'} '{pf.get('title') or ''}'"
        if not matches or not active:
            invalid.append(f"safety floor: verified {sev_of(pf)} {label} is missing from the merged set")
        else:
            ranks = [RANK.get(sev_of(m), -1) if isinstance(sev_of(m), str) else -1 for m in active]
            if max(ranks) < RANK[sev_of(pf)]:
                eval_ok = (
                    not is_floor_protected(pf)
                    and any(m.get("eval_downgraded") is True and present(m.get("eval_reason")) for m in active)
                )
                declined_standing = any(
                    m.get("status") == "declined" and m.get("declined_still_present") is True
                    for m in active
                )
                if not eval_ok and not declined_standing:
                    invalid.append(f"safety floor: verified {sev_of(pf)} {label} was lowered in the merged set")


def check_previous(prev_findings, findings, lanes, invalid, notes, level="PR"):
    cleared = {}
    for ln in lanes:
        if isinstance(ln, dict) and isinstance(ln.get("cleared"), list):
            for c in ln["cleared"]:
                if isinstance(c, dict) and is_str_present(c.get("id")) and present(c.get("evidence")):
                    cleared[c["id"]] = c["evidence"]
    by_id = {f["id"]: f for f in findings if isinstance(f, dict) and is_str_present(f.get("id"))}
    legacy = 0
    for pf in prev_findings:
        if not isinstance(pf, dict):
            continue
        if pf.get("status") is None:
            legacy += 1
            continue
        psev = sev_of(pf)
        if not isinstance(psev, str) or psev not in ("CRITICAL", "HIGH") or status_of(pf) != "todo" \
                or origin_of(pf) != "new" or excluded(pf, level=level):
            continue
        pid = pf.get("id")
        if not isinstance(pid, str):
            continue
        cur = by_id.get(pid)
        if cur is None or excluded(cur, level=level):
            if pid not in cleared:
                invalid.append(f"{pid}: previous open {psev} is not raised again, "
                               "carried, fixed, declined or cleared with evidence")
        elif status_of(cur) == "todo" and (
            not isinstance(sev_of(cur), str) or RANK.get(sev_of(cur), -1) < RANK[psev]
        ):
            invalid.append(f"{pid}: previous open {psev} is now {sev_of(cur)}")
    if legacy:
        notes.append(f"previous: {legacy} finding(s) have no status, so the carry check skipped them")


def _parse_bool_opt(opt_val, data_val, name, invalid):
    if opt_val:
        return True
    if data_val is None:
        return False
    if isinstance(data_val, bool):
        return data_val
    invalid.append(f"{name} must be a boolean")
    return False


def compute(data, opts, pre=None, previous=None):
    invalid, notes, reasons = [], [], []
    if isinstance(data, list):
        data = {"findings": data}
    if not isinstance(data, dict):
        return {"valid": False, "invalid": ["input is not a JSON object or array"]}
    raw_level = opts.get("level") if opts.get("level") is not None else data.get("level")
    level = (str(raw_level) if raw_level is not None else "PR").upper()
    effort = opts.get("effort") if opts.get("effort") is not None else (data.get("effort") or "high")
    hold = opts.get("hold_event") if opts.get("hold_event") is not None else (data.get("hold_event") or "REQUEST_CHANGES")
    me = _parse_bool_opt(opts.get("author_is_me"), data.get("author_is_me"), "author_is_me", invalid)
    in_profile = _parse_bool_opt(opts.get("in_profile"), data.get("in_profile"), "in_profile", invalid)
    ci = opts.get("ci") if opts.get("ci") is not None else data.get("ci")
    if ci is not None and not isinstance(ci, str):
        invalid.append("ci must be a string")
        ci = None
    if level not in REVIEW_LEVELS:
        invalid.append(f"level is not one of {', '.join(REVIEW_LEVELS)}")
        level = "PR"
    if not isinstance(effort, str) or effort not in LEVELS:
        invalid.append(f"effort is not one of {', '.join(LEVELS)}")
        effort = "high"
    if not isinstance(hold, str) or hold not in HOLD_EVENTS:
        invalid.append("hold_event is not REQUEST_CHANGES or COMMENT")
        hold = "REQUEST_CHANGES"
    if level == "CODEBASE" and effort == "skim":
        invalid.append("skim effort is not supported at level CODEBASE")

    raw_findings = data.get("findings")
    if raw_findings is not None and not isinstance(raw_findings, list):
        invalid.append("findings must be a list")
        findings = []
    else:
        findings = raw_findings or []

    raw_ac = data.get("ac_matrix")
    if raw_ac is not None and not isinstance(raw_ac, list):
        invalid.append("ac_matrix must be a list")
        ac_rows = []
    else:
        ac_rows = raw_ac or []

    lanes = data.get("lanes")
    if lanes is None:
        if effort in ("high", "medium"):
            invalid.append("lanes is missing; the lane list is needed at high and medium")
        lanes = []
    elif not isinstance(lanes, list):
        invalid.append("lanes must be a list")
        lanes = []

    raw_folded = data.get("folded")
    if raw_folded is not None and not isinstance(raw_folded, list):
        invalid.append("folded must be a list")
        folded = []
    else:
        folded = raw_folded or []

    if effort == "skim" and ac_rows:
        notes.append("skim has no AC matrix; the ac_matrix rows were ignored")
        ac_rows = []

    validate(findings, ac_rows, lanes, effort, invalid, level=level)
    check_ac_named(findings, ac_rows, invalid, level=level)
    if pre is not None:
        if isinstance(pre, list) or (isinstance(pre, dict) and (pre.get("findings") is None or isinstance(pre.get("findings"), list))):
            check_floor(findings_of(pre), findings, invalid)
        else:
            invalid.append("pre file must be a JSON object with a findings list or an array")
    if previous is not None:
        if isinstance(previous, list) or (isinstance(previous, dict) and (previous.get("findings") is None or isinstance(previous.get("findings"), list))):
            check_previous(findings_of(previous), findings, lanes, invalid, notes, level=level)
        else:
            invalid.append("previous file must be a JSON object with a findings list or an array")

    live = [f for f in findings if isinstance(f, dict) and isinstance(f.get("severity"), str)
            and f["severity"] in SEVS and not excluded(f, level=level)]
    open_set = [f for f in live if origin_of(f) == "new" and status_of(f) == "todo"]
    declined = [
        f for f in live
        if origin_of(f) == "new" and status_of(f) == "declined"
        and (sev_of(f) in ("CRITICAL", "HIGH") or f.get("declined_still_present") is True)
    ]
    n = {s: sum(1 for f in open_set if sev_of(f) == s) for s in SEVS}
    open_counts = {s.lower(): n[s] for s in SEVS}
    unsat = sum(1 for r in ac_rows if isinstance(r, dict) and r.get("status") in ("Missing", "Contradicted"))
    nomig = any(breaking_without_migration(f) for f in open_set)

    states = [lane_state(ln) for ln in lanes if isinstance(ln, dict)]
    complete = "NOT ASSESSED" not in states
    if effort == "skim" and any(isinstance(ln, dict) and ln.get("lane") == "skim"
                                and lane_state(ln) == "NOT ASSESSED" for ln in lanes):
        invalid.append("skim lane failed; this review has no verdict")

    # Team-gate band (SKILL.md Phase 2.4).
    band = "n/a"
    if effort in ("high", "medium"):
        names = {ln.get("lane") for ln in lanes if isinstance(ln, dict) and isinstance(ln.get("lane"), str)}
        ran = [b for b in BAND_LANES if b in names]
        if ran:
            worst = 0
            for b in ran:
                sevs = {sev_of(f) for f in open_set + declined if b in finding_lanes(f)}
                lvl = 2 if sevs & {"CRITICAL", "HIGH"} else 1 if "MEDIUM" in sevs else 0
                worst = max(worst, lvl)
            if worst == 0 and not complete:
                worst = 1
            band = ("PASS", "CONCERNS", "FAIL")[worst]
            if effort == "medium" and folded:
                band += " (partial)"

    # Verdict (output/verdict.md). One table at every effort: effort never changes it.
    if n["CRITICAL"]:
        verdict, why = "REQUEST CHANGES", "open CRITICAL"
    elif unsat:
        verdict, why = "REQUEST CHANGES", "Missing or Contradicted AC"
    elif nomig:
        verdict, why = "REQUEST CHANGES", "breaking change with no migration"
    elif n["HIGH"]:
        verdict, why = "REQUEST CHANGES", "open HIGH"
    elif n["MEDIUM"] or (n["QUESTION"] and not QUESTION_ONLY_APPROVES):
        verdict, why = "APPROVE WITH COMMENTS", "MEDIUM or QUESTION"
    else:
        verdict, why = "APPROVE", "only LOW, or nothing"
    reasons.append(why)
    if verdict == "APPROVE" and declined:
        verdict = "APPROVE WITH COMMENTS"
        reasons.append("declined_blocking: a declined new CRITICAL or HIGH blocks APPROVE")
    if not complete and verdict == "APPROVE":
        # An incomplete review cannot approve. The event follows the verdict below.
        verdict = "APPROVE WITH COMMENTS"
        failed = sorted(str(ln.get("lane", "?")) for ln in lanes
                        if isinstance(ln, dict) and lane_state(ln) == "NOT ASSESSED")
        reasons.append("incomplete: NOT ASSESSED lane(s) " + ", ".join(failed) + " block APPROVE")

    if verdict == "REQUEST CHANGES":
        next_step = "fix"
    elif not complete:
        next_step = "rerun"
    elif verdict == "APPROVE WITH COMMENTS":
        next_step = "document"
    else:
        next_step = "done"

    if level == "CODEBASE":
        verdict = CODEBASE_VERDICT_MAP[verdict]
        emoji, phrase = CODEBASE_PHRASE[verdict]
        event = "NONE"
    else:
        emoji, phrase, me_phrase = PHRASE[verdict]
        if me:
            phrase = me_phrase
        if me:
            event = "COMMENT"
        elif verdict == "REQUEST CHANGES":
            event = hold
        elif verdict == "APPROVE WITH COMMENTS":
            event = "COMMENT"
        else:
            event = "APPROVE"

    return {
        "valid": not invalid,
        "level": level,
        "effort": effort,
        "open": open_counts,
        "unsatisfied": unsat,
        "declined_blocking": len(declined),
        "complete": complete,
        "band": band,
        "verdict": verdict,
        "emoji": emoji,
        "phrase": phrase,
        "event": event,
        "next_step": next_step,
        "reasons": reasons,
        "notes": notes,
        "invalid": invalid,
    }


def load(path, what, invalid):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        invalid.append(f"cannot read {what} {path}: {exc.__class__.__name__}")
        return None


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="breaker.py", description=__doc__.split("\n")[0])
    ap.add_argument("findings")
    ap.add_argument("--pre")
    ap.add_argument("--previous")
    ap.add_argument("--effort", choices=LEVELS)
    ap.add_argument("--level", type=str.upper, choices=REVIEW_LEVELS)
    ap.add_argument("--hold-event", dest="hold_event", choices=HOLD_EVENTS)
    ap.add_argument("--author-is-me", dest="author_is_me", action="store_true")
    ap.add_argument("--in-profile", dest="in_profile", action="store_true")
    ap.add_argument("--ci")
    args = ap.parse_args(argv)

    bad = []
    data = load(args.findings, "findings", bad)
    pre = load(args.pre, "pre file", bad) if args.pre else None
    prev = load(args.previous, "previous file", bad) if args.previous else None
    if bad or data is None:
        out = {"valid": False, "open": None, "unsatisfied": None, "complete": None, "band": None,
               "verdict": None, "event": None, "next_step": None, "invalid": bad}
    else:
        out = compute(data, vars(args), pre, prev)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    for line in out.get("invalid") or []:
        print("INVALID: " + line, file=sys.stderr)
    return 0 if out.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
