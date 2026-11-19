#!/usr/bin/env python3
"""Deterministic breaker: open counts, completeness, quality-gate band, verdict and event.

Usage:
  breaker.py <findings.json> [--pre <pre.json>] [--previous <previous.json>]
             [--effort high|medium|low|skim] [--hold-event REQUEST_CHANGES|COMMENT]
             [--author-is-me] [--in-profile] [--ci <value>]

Input (<findings.json>): a JSON object, or a bare JSON array of findings:
  {"findings": [Finding...],          merged and verified set (SKILL.md Phase 2.2 and 2.3)
   "ac_matrix": [ACRow...],           statuses after verification
   "lanes": [{"lane", "status"?, "reason"?, "high_risk"?, "cleared"?}...],
   "folded": ["security", ...],       medium only: lanes folded into conventions
   "effort", "hold_event", "author_is_me", "in_profile", "ci"   optional; flags override}
A lane status is returned (default), abstained, or NOT ASSESSED (alias: failed).
An abstained lane counts as returned.

Findings that do not count: verified "refuted", introduced_by_pr "pre-existing",
origin "existing". A finding with no status counts as todo, and one with no origin
counts as new. The lead removes chat-only items before the call.

Output: JSON on stdout:
  {"valid", "effort", "open", "unsatisfied", "declined_blocking", "complete", "band",
   "verdict", "emoji", "phrase", "event", "reasons", "notes", "invalid"}
Exit code: 0 when valid. 1 when invalid, with one "INVALID: ..." line on stderr for
each problem. 2 for a usage error. The script never writes a file and never changes
the findings. The verdict and the event come from this script only. Do not recompute them.
Stdlib only.

Verdict rules (moved here from reference/output/verdict.md; the code below is the authority):
  high and medium, checked in order:
    any verified CRITICAL, any Missing/Contradicted AC, any breaking change with no
    migration, or HIGH > 3              -> REQUEST CHANGES, event voice.hold_event (default REQUEST_CHANGES)
    any HIGH, MEDIUM or QUESTION        -> APPROVE WITH COMMENTS, event COMMENT
    only LOW, or nothing                -> APPROVE, event APPROVE
    The verdict uses the full merged set, before the medium Pareto cut.
  low: the low verdict phrases (reference/effort/low.md, Low report); event always COMMENT.
  skim: the Skim verdict table (reference/effort/skim.md); APPROVE only when the PR is in
    profile, CI is passing and not author_is_me.
  Every level: a NOT ASSESSED lane caps the verdict at APPROVE WITH COMMENTS (the reason
    names the lane). A new CRITICAL or HIGH with status declined counts in
    declined_blocking and blocks APPROVE. author_is_me makes the event COMMENT.
  Quality gate (high and medium): band code-quality, performance and security; CRITICAL or
    HIGH -> FAIL, MEDIUM -> CONCERNS, LOW or none -> PASS; the worst band wins. A NOT
    ASSESSED lane sets complete false and caps the band at CONCERNS. Medium adds
    "(partial)" when a lane was folded. Low and skim: band n/a.
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
FLOOR_KEYWORDS = ("data loss", "data corruption", "corrupt", "injection", "auth")

# Open point A1 in the final report: output/verdict.md §Verdict says both "any QUESTION ->
# APPROVE WITH COMMENTS" (table) and "only QUESTION -> APPROVE" (bullet). The table and the
# skim table agree, and APPROVE WITH COMMENTS is the stricter result. Set True to follow the bullet.
QUESTION_ONLY_APPROVES = False

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
LOW_HIGH_PHRASE = "Fix the HIGH items before merge"
LOW_NONE_PHRASE = ("No CRITICAL or HIGH issues found. "
                   "This review did not examine MEDIUM or LOW issues.")


def present(v):
    return v is not None and str(v).strip() != ""


def sev_of(f):
    return f.get("severity")


def status_of(f):
    return f.get("status") or "todo"


def origin_of(f):
    return f.get("origin") or "new"


def excluded(f, level="PR"):
    if f.get("verified") == "refuted" or f.get("eval_disposition") in ("drop", "chat-only"):
        return True
    if level == "PR" and f.get("introduced_by_pr") == "pre-existing":
        return True
    return False


def tokens(text):
    return {t.upper() for t in re.split(r"[\s,;/()]+", str(text or "")) if t}


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
        return obj.get("findings") or []
    return obj if isinstance(obj, list) else []


def lane_state(entry):
    return "NOT ASSESSED" if entry.get("status") in NOT_ASSESSED else entry.get("status") or "returned"


def validate(findings, ac_rows, lanes, effort, invalid, level="PR"):
    """Structure and enum checks, including the optional contract fields."""
    seen = set()
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            invalid.append(f"finding #{i} is not an object")
            continue
        fid = f.get("id") or f"#{i}"
        if f.get("severity") not in SEVS:
            invalid.append(f"{fid}: severity is not one of {', '.join(SEVS)}")
        if present(f.get("id")):
            if f["id"] in seen:
                invalid.append(f"{fid}: duplicate id")
            seen.add(f["id"])
        if f.get("status") is not None and f["status"] not in FINDING_STATUS:
            invalid.append(f"{fid}: status is not one of todo, fixed, declined")
        if f.get("origin") is not None and f["origin"] not in ORIGINS:
            invalid.append(f"{fid}: origin is not one of new, existing")
        if f.get("status") == "fixed" and not present(f.get("commit")):
            invalid.append(f"{fid}: status fixed needs a commit")
        if f.get("status") == "declined" and not present(f.get("reason")):
            invalid.append(f"{fid}: status declined needs a reason")
        if f.get("eval_disposition") is not None and f["eval_disposition"] not in EVAL_DISPOSITIONS:
            invalid.append(f"{fid}: eval_disposition is not one of post, chat-only, drop")
        if f.get("eval_downgraded") is not None and not isinstance(f["eval_downgraded"], bool):
            invalid.append(f"{fid}: eval_downgraded must be a boolean")
        if f.get("eval_downgraded") is True and not present(f.get("eval_reason")):
            invalid.append(f"{fid}: eval_downgraded needs eval_reason")
        if excluded(f, level=level):
            continue
        if sev_of(f) in ("CRITICAL", "HIGH"):
            if not has_trigger(f):
                invalid.append(f"{fid}: {sev_of(f)} has no trigger")
            if f.get("verified") == "unverified":
                invalid.append(f"{fid}: {sev_of(f)} is unverified; downgrade it to QUESTION (Phase 2.3)")
        if effort == "skim" and sev_of(f) in ("MEDIUM", "LOW"):
            invalid.append(f"{fid}: {sev_of(f)} must be dropped at skim")
    ids = {f.get("id"): f for f in findings if isinstance(f, dict) and present(f.get("id"))}
    for f in findings:
        if isinstance(f, dict) and present(f.get("challenges")):
            target = ids.get(f["challenges"])
            if target is None or target.get("status") != "declined":
                invalid.append(f"{f.get('id') or '?'}: challenges {f['challenges']}, "
                               "which is not a declined finding in this set")
    for i, r in enumerate(ac_rows):
        if not isinstance(r, dict) or not present(r.get("ref")):
            invalid.append(f"ac_matrix row #{i} has no ref")
        elif r.get("status") not in AC_STATUS:
            invalid.append(f"{r['ref']}: AC status is not one of {', '.join(sorted(AC_STATUS))}")
    for i, ln in enumerate(lanes):
        if not isinstance(ln, dict) or ln.get("lane") not in LANE_NAMES:
            invalid.append(f"lane #{i} has an unknown lane name")
            continue
        if ln.get("status") is not None and ln["status"] not in LANE_STATUS:
            invalid.append(f"{ln['lane']}: lane status is not one of returned, abstained, NOT ASSESSED")
        st = lane_state(ln)
        if st in ("abstained", "NOT ASSESSED") and not present(ln.get("reason")):
            invalid.append(f"{ln['lane']}: lane status {st} needs a reason")
        for c in ln.get("cleared") or []:
            if not isinstance(c, dict) or not present(c.get("id")) or not present(c.get("evidence")):
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
            if str(r.get("ref", "")).upper() not in named:
                invalid.append(f"{r.get('ref')}: AC is {r['status']} but no finding names it in ac_ref")


def loc_sites(f):
    out = []
    for loc in [f.get("location")] + list(f.get("locations") or []):
        m = re.match(r"^(?P<p>[^\s:]+):(?P<a>\d+)(?:-(?P<b>\d+))?$", str(loc or "").strip())
        if m:
            p = m.group("p")
            while p.startswith("head/"):
                p = p[5:]
            out.append((p, int(m.group("a")), int(m.group("b") or m.group("a"))))
    return out


def same_flaw(pre, post):
    if present(pre.get("id")) and pre.get("id") == post.get("id"):
        return True
    t = str(pre.get("title") or "").strip().lower()
    names = [str(post.get("title") or "").strip().lower()] + \
            [str(x).strip().lower() for x in post.get("merged_titles") or []]
    if t and t in names:
        return True
    for p, a, b in loc_sites(pre):
        for q, c, d in loc_sites(post):
            if p == q and a - 3 <= d and c - 3 <= b:
                return True
    return False


def is_floor_protected(pf):
    """Return True when a confirmed finding is strictly protected from evaluation drops or downgrades."""
    if pf.get("verified") != "confirmed":
        return False
    if sev_of(pf) == "CRITICAL":
        return True
    if sev_of(pf) != "HIGH":
        return False
    if pf.get("lane") == "security" or breaking_without_migration(pf) or present(pf.get("ac_ref")):
        return True
    text = (str(pf.get("dimension") or "") + " " + str(pf.get("impact") or "")).lower()
    return any(kw in text for kw in FLOOR_KEYWORDS)


def check_floor(pre_findings, findings, invalid):
    """Safety floor: a verified CRITICAL or HIGH of the pre-merge set stays, at the same level or higher.
    Non-floor HIGH findings may be downgraded only by evaluation with eval_downgraded=True and eval_reason."""
    for pf in pre_findings:
        if not isinstance(pf, dict) or pf.get("verified") != "confirmed":
            continue
        if sev_of(pf) not in ("CRITICAL", "HIGH"):
            continue
        matches = [f for f in findings if isinstance(f, dict) and same_flaw(pf, f)]
        active = [m for m in matches if m.get("eval_disposition") not in ("drop", "chat-only")]
        label = f"{pf.get('id') or '?'} '{pf.get('title') or ''}'"
        if not matches or not active:
            invalid.append(f"safety floor: verified {sev_of(pf)} {label} is missing from the merged set")
        elif max(RANK.get(sev_of(m), -1) for m in active) < RANK[sev_of(pf)]:
            eval_ok = (
                not is_floor_protected(pf)
                and any(m.get("eval_downgraded") is True and present(m.get("eval_reason")) for m in active)
            )
            if not eval_ok:
                invalid.append(f"safety floor: verified {sev_of(pf)} {label} was lowered in the merged set")


def check_previous(prev_findings, findings, lanes, invalid, notes, level="PR"):
    cleared = {}
    for ln in lanes:
        if isinstance(ln, dict):
            for c in ln.get("cleared") or []:
                if isinstance(c, dict) and present(c.get("id")) and present(c.get("evidence")):
                    cleared[c["id"]] = c["evidence"]
    by_id = {f.get("id"): f for f in findings if isinstance(f, dict) and present(f.get("id"))}
    legacy = 0
    for pf in prev_findings:
        if not isinstance(pf, dict):
            continue
        if pf.get("status") is None:
            legacy += 1
            continue
        if sev_of(pf) not in ("CRITICAL", "HIGH") or status_of(pf) != "todo" \
                or origin_of(pf) != "new" or excluded(pf, level=level):
            continue
        cur = by_id.get(pf.get("id"))
        if cur is None:
            if pf.get("id") not in cleared:
                invalid.append(f"{pf.get('id')}: previous open {sev_of(pf)} is not raised again, "
                               "carried, fixed, declined or cleared with evidence")
        elif status_of(cur) == "todo" and RANK.get(sev_of(cur), -1) < RANK[sev_of(pf)]:
            invalid.append(f"{pf.get('id')}: previous open {sev_of(pf)} is now {sev_of(cur)}")
    if legacy:
        notes.append(f"previous: {legacy} finding(s) have no status, so the carry check skipped them")


def compute(data, opts, pre=None, previous=None):
    invalid, notes, reasons = [], [], []
    if isinstance(data, list):
        data = {"findings": data}
    if not isinstance(data, dict):
        return {"valid": False, "invalid": ["input is not a JSON object or array"]}
    level = str(opts.get("level") or data.get("level") or "PR").upper()
    effort = opts.get("effort") or data.get("effort") or "high"
    hold = opts.get("hold_event") or data.get("hold_event") or "REQUEST_CHANGES"
    me = bool(opts.get("author_is_me") or data.get("author_is_me"))
    in_profile = bool(opts.get("in_profile") or data.get("in_profile"))
    ci = opts.get("ci") if opts.get("ci") is not None else data.get("ci")
    if level not in REVIEW_LEVELS:
        invalid.append(f"level is not one of {', '.join(REVIEW_LEVELS)}")
        level = "PR"
    if effort not in LEVELS:
        invalid.append(f"effort is not one of {', '.join(LEVELS)}")
        effort = "high"
    if hold not in HOLD_EVENTS:
        invalid.append("hold_event is not REQUEST_CHANGES or COMMENT")
        hold = "REQUEST_CHANGES"

    findings = findings_of(data)
    ac_rows = data.get("ac_matrix") or []
    lanes = data.get("lanes")
    folded = data.get("folded") or []
    if lanes is None:
        if effort in ("high", "medium"):
            invalid.append("lanes is missing; the lane list is needed at high and medium")
        lanes = []
    if effort == "skim" and ac_rows:
        notes.append("skim has no AC matrix; the ac_matrix rows were ignored")
        ac_rows = []

    validate(findings, ac_rows, lanes, effort, invalid, level=level)
    check_ac_named(findings, ac_rows, invalid, level=level)
    if pre is not None:
        check_floor(findings_of(pre), findings, invalid)
    if previous is not None:
        check_previous(findings_of(previous), findings, lanes, invalid, notes, level=level)

    live = [f for f in findings if isinstance(f, dict) and f.get("severity") in SEVS and not excluded(f, level=level)]
    open_set = [f for f in live if origin_of(f) == "new" and status_of(f) == "todo"]
    declined = [f for f in live if origin_of(f) == "new" and status_of(f) == "declined"
                and sev_of(f) in ("CRITICAL", "HIGH")]
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
        names = {ln.get("lane") for ln in lanes if isinstance(ln, dict)}
        ran = [b for b in BAND_LANES if b in names]
        if ran:
            worst = 0
            for b in ran:
                sevs = {sev_of(f) for f in open_set + declined if f.get("lane") == b}
                lvl = 2 if sevs & {"CRITICAL", "HIGH"} else 1 if "MEDIUM" in sevs else 0
                worst = max(worst, lvl)
            if worst == 0 and not complete:
                worst = 1
            band = ("PASS", "CONCERNS", "FAIL")[worst]
            if effort == "medium" and folded:
                band += " (partial)"

    # Verdict and event (output/verdict.md §Verdict, effort/low.md §Low verdict phrases, §Skim verdict).
    if effort in ("high", "medium"):
        if n["CRITICAL"]:
            verdict, why = "REQUEST CHANGES", "verified CRITICAL"
        elif unsat:
            verdict, why = "REQUEST CHANGES", "Missing or Contradicted AC"
        elif nomig:
            verdict, why = "REQUEST CHANGES", "breaking change with no migration"
        elif n["HIGH"] > 3:
            verdict, why = "REQUEST CHANGES", "HIGH > 3"
        elif n["HIGH"] or n["MEDIUM"] or (n["QUESTION"] and not QUESTION_ONLY_APPROVES):
            verdict, why = "APPROVE WITH COMMENTS", "HIGH, MEDIUM or QUESTION"
        else:
            verdict, why = "APPROVE", "only LOW, or nothing"
    elif effort == "low":
        if n["CRITICAL"] or unsat:
            verdict, why = "REQUEST CHANGES", "verified CRITICAL or Missing or Contradicted AC"
        elif n["HIGH"]:
            verdict, why = "REQUEST CHANGES", "other HIGH"
        else:
            verdict, why = "APPROVE", "nothing at CRITICAL or HIGH"
    else:
        if n["CRITICAL"] or n["HIGH"]:
            verdict, why = "REQUEST CHANGES", "verified CRITICAL or HIGH"
        elif n["QUESTION"]:
            verdict, why = "APPROVE WITH COMMENTS", "QUESTION"
        else:
            verdict, why = "APPROVE", "nothing"
    reasons.append(why)
    if verdict == "APPROVE" and declined:
        verdict = "APPROVE WITH COMMENTS"
        reasons.append("declined_blocking: a declined new CRITICAL or HIGH blocks APPROVE")
    if not complete and verdict == "APPROVE":
        # An incomplete review cannot approve. The event follows the verdict below.
        verdict = "APPROVE WITH COMMENTS"
        failed = sorted(ln.get("lane", "?") for ln in lanes
                        if isinstance(ln, dict) and lane_state(ln) == "NOT ASSESSED")
        reasons.append("incomplete: NOT ASSESSED lane(s) " + ", ".join(failed) + " block APPROVE")

    if level == "CODEBASE":
        verdict = CODEBASE_VERDICT_MAP[verdict]
        emoji, phrase = CODEBASE_PHRASE[verdict]
        event = "NONE"
    else:
        emoji, phrase, me_phrase = PHRASE[verdict]
        if effort == "low" and not me:
            if verdict == "REQUEST CHANGES" and why == "other HIGH":
                phrase = LOW_HIGH_PHRASE
            elif verdict == "APPROVE":
                emoji, phrase = "🟢", LOW_NONE_PHRASE
        if me:
            phrase = me_phrase

        if me or effort == "low":
            event = "COMMENT"
        elif verdict == "REQUEST CHANGES":
            if effort == "skim":
                event = hold if n["CRITICAL"] else "COMMENT"
            else:
                event = hold
        elif verdict == "APPROVE WITH COMMENTS":
            event = "COMMENT"
        elif effort == "skim":
            event = "APPROVE" if (in_profile and ci == "passing") else "COMMENT"
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
               "verdict": None, "event": None, "invalid": bad}
    else:
        out = compute(data, vars(args), pre, prev)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    for line in out.get("invalid") or []:
        print("INVALID: " + line, file=sys.stderr)
    return 0 if out.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
