#!/usr/bin/env python3
"""Deterministic Phase 2 merge, gates G1-G5, dedupe, verifier batching, evaluation finalize, Carry G9, Breaker, and Pareto cut.

Usage:
  merge_findings.py merge    <bundle_dir> [--raw <raw-lanes.json>] [--effort high|medium|low|skim] [--level PR|CODEBASE]
  merge_findings.py finalize <bundle_dir> [--verifier <verifier-results.json>] [--eval <eval-results.json>]
                             [--effort high|medium|low|skim] [--level PR|CODEBASE] [--hold-event REQUEST_CHANGES|COMMENT]
                             [--author-is-me] [--in-profile] [--ci <value>] [--carry] [--still-open <file>]

Imports sibling schema_lint, carry_findings, and breaker in-process (zero subprocess calls).
Python standard library only.
"""
import argparse
import copy
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import breaker  # noqa: E402
import carry_findings  # noqa: E402
import schema_lint  # noqa: E402

SEV_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "QUESTION": 0}
SEV_DOWN = {"CRITICAL": "HIGH", "HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW", "QUESTION": "QUESTION"}
SEV_UP = {"CRITICAL": "CRITICAL", "HIGH": "CRITICAL", "MEDIUM": "HIGH", "LOW": "MEDIUM", "QUESTION": "MEDIUM"}

PARETO_SEV_W = {"CRITICAL": 8.0, "HIGH": 5.0, "MEDIUM": 3.0, "QUESTION": 2.0, "LOW": 1.0}
PARETO_CONF_W = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}
HIGH_REACH_RX = re.compile(r"(auth|migration|model|schema|route|api|controller|shared|core|public|payment|billing)", re.I)

FENCED_JSON_RX = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.S)
LOC_RX = re.compile(r"^(?P<path>[^\s:]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
STOP = set(
    "the and for with that this from are not but has have was were when then than into "
    "its can will may all any one two new old use used uses get gets set sets via per should must".split()
)


def load_json(path, default=None):
    if not path or not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def norm_path(p):
    p = str(p or "").strip()
    while p.startswith("head/"):
        p = p[5:]
    return p


def norm_loc(loc):
    s = str(loc or "").strip()
    while s.startswith("head/"):
        s = s[5:]
    return s


def parse_loc(loc):
    m = LOC_RX.match(norm_loc(loc))
    if not m:
        return None, None, None
    a = int(m.group("a"))
    return m.group("path"), a, int(m.group("b") or a)


def tokenize(text):
    out = set()
    for w in re.findall(r"[a-z0-9_]{3,}", str(text or "").lower()):
        if w in STOP:
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def jaccard(a_text, b_text):
    ta, tb = tokenize(a_text), tokenize(b_text)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def parse_lane_entry(item, lane_hint=None):
    """Normalize a lane output entry (JSON dict or raw text containing fenced JSON)."""
    if isinstance(item, str):
        m = FENCED_JSON_RX.search(item)
        if m:
            try:
                item = json.loads(m.group(1))
            except ValueError:
                return {"lane": lane_hint or "unknown", "status": "NOT ASSESSED", "reason": "unparseable lane JSON", "findings": []}
        else:
            try:
                item = json.loads(item)
            except ValueError:
                return {"lane": lane_hint or "unknown", "status": "NOT ASSESSED", "reason": "missing JSON lane block", "findings": []}
    if not isinstance(item, dict):
        return {"lane": lane_hint or "unknown", "status": "NOT ASSESSED", "reason": "lane output is not an object", "findings": []}
    out = dict(item)
    if lane_hint and not out.get("lane"):
        out["lane"] = lane_hint
    out.setdefault("status", "returned")
    out.setdefault("findings", [])
    return out


def load_raw_lanes(raw_obj):
    if isinstance(raw_obj, list):
        return [parse_lane_entry(x) for x in raw_obj]
    if isinstance(raw_obj, dict):
        if "lanes" in raw_obj and isinstance(raw_obj["lanes"], list):
            return [parse_lane_entry(x) for x in raw_obj["lanes"]]
        return [parse_lane_entry(v, lane_hint=k) for k, v in raw_obj.items()]
    return []


def matches_existing_comment(f, existing):
    """Gate G5: check whether an inline comment covers same file within +-5 lines with >=0.35 word overlap."""
    if f.get("severity") in ("CRITICAL", "HIGH"):
        return False
    fpath, fa, fb = parse_loc(f.get("location"))
    if not fpath or fa is None:
        return False
    ftext = f"{f.get('title', '')} {f.get('issue', '')}"
    inline_list = existing.get("inline", []) if isinstance(existing, dict) else (existing if isinstance(existing, list) else [])
    for c in inline_list:
        if not isinstance(c, dict):
            continue
        cpath = norm_path(c.get("path"))
        cline = c.get("line")
        if cpath != fpath or not isinstance(cline, int):
            continue
        if fa - 5 <= cline <= fb + 5 and jaccard(ftext, c.get("body", "")) >= 0.35:
            return True
    return False


def can_merge_findings(a, b):
    dk_a, dk_b = str(a.get("dedupe_key") or "").strip(), str(b.get("dedupe_key") or "").strip()
    if dk_a and dk_b and dk_a == dk_b:
        return True
    pa, a1, a2 = parse_loc(a.get("location"))
    pb, b1, b2 = parse_loc(b.get("location"))
    if not pa or pa != pb or a1 is None or b1 is None:
        return False
    if max(a1, b1) - min(a2, b2) > 3:
        return False
    ta = f"{a.get('title', '')} {a.get('issue', '')}"
    tb = f"{b.get('title', '')} {b.get('issue', '')}"
    return jaccard(ta, tb) >= 0.40


def dedupe_findings(findings):
    merged = []
    for f in findings:
        f = copy.deepcopy(f)
        lane_src = f.get("lane")
        srcs = list(f.get("sources") or [])
        if lane_src and lane_src not in srcs:
            srcs.append(lane_src)
        f["sources"] = srcs

        target = None
        for m in merged:
            if can_merge_findings(m, f):
                target = m
                break
        if target is None:
            merged.append(f)
            continue

        # Merge f into target: keep higher severity, union sources & locations, record merged_titles
        if SEV_RANK.get(f.get("severity"), -1) > SEV_RANK.get(target.get("severity"), -1):
            old_title = target.get("title")
            old_sources = list(target.get("sources") or [])
            old_locs = [target.get("location")] + list(target.get("locations") or [])
            old_merged = list(target.get("merged_titles") or [])
            target.clear()
            target.update(f)
            for s in old_sources:
                if s not in target["sources"]:
                    target["sources"].append(s)
            extra_locs = list(target.get("locations") or [])
            for loc in old_locs:
                if loc and loc != target.get("location") and loc not in extra_locs:
                    extra_locs.append(loc)
            if extra_locs:
                target["locations"] = extra_locs
            mt = list(target.get("merged_titles") or []) + old_merged
            if old_title and old_title != target.get("title") and old_title not in mt:
                mt.append(old_title)
            if mt:
                target["merged_titles"] = mt
        else:
            for s in f["sources"]:
                if s not in target["sources"]:
                    target["sources"].append(s)
            extra_locs = list(target.get("locations") or [])
            for loc in [f.get("location")] + list(f.get("locations") or []):
                if loc and loc != target.get("location") and loc not in extra_locs:
                    extra_locs.append(loc)
            if extra_locs:
                target["locations"] = extra_locs
            mt = list(target.get("merged_titles") or [])
            ftitle = f.get("title")
            if ftitle and ftitle != target.get("title") and ftitle not in mt:
                mt.append(ftitle)
            if mt:
                target["merged_titles"] = mt
    return merged


def first_sentence(text):
    s = str(text or "").strip()
    m = re.split(r"(?<=[.!?])\s+", s, maxsplit=1)
    return m[0] if m else s


def build_verify_batches(findings, ac_rows, effort, level="PR"):
    """Group CRITICAL/HIGH findings and Missing/Contradicted ACs by file into capped blind-verifier batches."""
    if effort in ("low", "skim"):
        return []
    cap = 2 if effort == "medium" else (8 if level == "CODEBASE" else 6)
    by_file = {}
    for f in findings:
        if f.get("severity") not in ("CRITICAL", "HIGH"):
            continue
        p, _, _ = parse_loc(f.get("location"))
        key = p or "__intent__"
        by_file.setdefault(key, []).append({
            "kind": "finding",
            "id": f["id"],
            "location": f["location"],
            "claim": f"{f.get('title', '')}: {first_sentence(f.get('issue', ''))}".strip(": "),
            "trigger": f.get("trigger", ""),
        })
    for r in ac_rows:
        if not isinstance(r, dict) or r.get("status") not in ("Missing", "Contradicted"):
            continue
        p, _, _ = parse_loc(r.get("evidence"))
        key = p or "__intent__"
        by_file.setdefault(key, []).append({
            "kind": "ac",
            "id": r.get("ref"),
            "location": r.get("evidence") or "intent.md",
            "claim": f"{r.get('ref')} ({r.get('status')}): {r.get('text', '')}",
            "trigger": r.get("note") or f"AC status reported as {r.get('status')}",
        })

    batches = [{"files": [k], "claims": v, "tier": "standard"} for k, v in sorted(by_file.items())]
    while len(batches) > cap:
        batches.sort(key=lambda b: len(b["claims"]))
        a = batches.pop(0)
        b = batches.pop(0)
        batches.append({
            "files": a["files"] + b["files"],
            "claims": a["claims"] + b["claims"],
            "tier": "standard",
        })
    for idx, batch in enumerate(batches, 1):
        batch["batch_id"] = f"verify-{idx}"
    return batches


def aggregate_lanes_meta(raw_lanes_meta):
    """Collapse sharded lane entries by logical lane name for breaker.py while preserving failure propagation."""
    grouped = {}
    order = []
    for m in raw_lanes_meta:
        ln = m["lane"]
        if ln not in grouped:
            grouped[ln] = []
            order.append(ln)
        grouped[ln].append(m)

    out = []
    for ln in order:
        items = grouped[ln]
        if len(items) == 1:
            one = {k: v for k, v in items[0].items() if k != "shard"}
            out.append(one)
            continue
        failed = [x for x in items if x.get("status") in ("NOT ASSESSED", "failed")]
        abstained = [x for x in items if x.get("status") == "abstained"]
        hr = next((x["high_risk"] for x in items if x.get("high_risk")), None)
        if failed:
            reasons = [
                f"{x.get('shard') or ln}: {x.get('reason') or 'shard failed'}"
                for x in failed
            ]
            row = {"lane": ln, "status": "NOT ASSESSED", "reason": "; ".join(reasons)}
        elif len(abstained) == len(items):
            reasons = [x.get("reason") or "abstained" for x in abstained]
            row = {"lane": ln, "status": "abstained", "reason": "; ".join(dict.fromkeys(reasons))}
        else:
            row = {"lane": ln, "status": "returned"}
        if hr:
            row["high_risk"] = hr
        out.append(row)
    return out


def cmd_merge(args):
    bundle_dir = os.path.abspath(args.bundle_dir)
    raw_path = args.raw or os.path.join(bundle_dir, "raw-lanes.json")
    raw_obj = load_json(raw_path)
    if raw_obj is None:
        sys.stderr.write(f"merge_findings: raw lanes file not found: {raw_path}\n")
        return 2
    summary_doc = load_json(os.path.join(bundle_dir, "bundle-summary.json"), default={})
    level = str(getattr(args, "level", None) or summary_doc.get("level") or "PR").upper()
    if level == "CODEBASE":
        loc_map = load_json(os.path.join(bundle_dir, "file-map.json"))
        if loc_map is None:
            loc_map = load_json(os.path.join(bundle_dir, "diff-map.json"), default={})
    else:
        loc_map = load_json(os.path.join(bundle_dir, "diff-map.json"), default={})
    existing = load_json(os.path.join(bundle_dir, "existing-comments.json"), default={"inline": [], "reviews": []})
    effort = args.effort or (summary_doc.get("effort") or {}).get("resolved") or "high"
    folded = [x.get("lane") for x in ((summary_doc.get("lane_plan") or {}).get("folded") or []) if isinstance(x, dict) and x.get("lane")]

    lane_entries = load_raw_lanes(raw_obj)
    raw_lanes_meta = []
    retry_lanes = {}
    ac_matrix = []
    high_risk = []
    safety_alerts = []
    raw_cleared = []
    other_titles = []
    surviving = []
    raw_count = 0
    counts = {"g2_out_of_diff": 0, "g3_downgraded": 0, "g4_low_conf": 0, "g5_already_raised": 0}

    for le in lane_entries:
        lname = le.get("lane") or "unknown"
        lstatus = le.get("status") or "returned"
        meta = {"lane": lname, "status": lstatus}
        if le.get("shard"):
            meta["shard"] = le["shard"]
        if le.get("reason"):
            meta["reason"] = le["reason"]
        if le.get("high_risk"):
            meta["high_risk"] = le["high_risk"]
            if le["high_risk"] not in high_risk:
                high_risk.append(le["high_risk"])
        raw_lanes_meta.append(meta)

        for sa in le.get("safety_alerts") or []:
            if sa and str(sa).lower() != "none" and sa not in safety_alerts:
                safety_alerts.append(sa)
        for c in le.get("cleared") or []:
            if isinstance(c, dict) and c.get("id") and c.get("evidence"):
                cc = dict(c)
                cc.setdefault("lane", lname)
                raw_cleared.append(cc)
        for ot in le.get("other") or []:
            if ot and str(ot).lower() != "none":
                other_titles.append(str(ot))
        if lname in ("adherence", "business-logic", "critical-risk"):
            for r in le.get("ac_matrix") or []:
                if isinstance(r, dict):
                    ac_matrix.append(r)

        if lstatus in ("abstained", "NOT ASSESSED", "failed"):
            continue

        for f in le.get("findings") or []:
            if not isinstance(f, dict):
                continue
            raw_count += 1
            f = copy.deepcopy(f)
            f.setdefault("lane", lname)
            if f.get("location"):
                f["location"] = norm_loc(f["location"])
            if isinstance(f.get("locations"), list):
                f["locations"] = [norm_loc(x) for x in f["locations"]]

            action, reasons, kept_locs = schema_lint.check_detail(f, loc_map, level=level)
            if action == "retry_lane":
                retry_key = le.get("shard") or lname
                retry_lanes.setdefault(retry_key, []).append({"id": f.get("id"), "reasons": reasons})
                continue
            if action == "drop":
                if any(r.startswith("G2") for r in reasons):
                    counts["g2_out_of_diff"] += 1
                else:
                    counts["g4_low_conf"] += 1
                continue
            if kept_locs:
                f["location"] = kept_locs[0]
                if len(kept_locs) > 1:
                    f["locations"] = kept_locs[1:]
                else:
                    f.pop("locations", None)
            if action == "downgrade":
                counts["g3_downgraded"] += 1
                f["severity"] = "QUESTION" if f.get("confidence") == "LOW" else "MEDIUM"
            f["in_diff"] = not bool(schema_lint.INTENT_LOC.match(str(f.get("location", ""))))

            # Gate G5 (already raised — PR mode only)
            if level == "PR" and matches_existing_comment(f, existing):
                counts["g5_already_raised"] += 1
                continue
            surviving.append(f)

    # Rules-changed automatic high_risk (PR mode only)
    if level == "PR" and summary_doc.get("rules_changed") and "rule files edited" not in high_risk:
        high_risk.append("rule files edited")

    lanes_meta = aggregate_lanes_meta(raw_lanes_meta)
    merged = dedupe_findings(surviving)
    # Remove any cleared entry whose id was raised again
    raised_ids = {f.get("id") for f in merged if f.get("id")}
    cleared = [c for c in raw_cleared if c.get("id") not in raised_ids]

    verify_batches = build_verify_batches(merged, ac_matrix, effort, level=level)
    nonblocking = [f for f in merged if f.get("severity") in ("MEDIUM", "LOW", "QUESTION")]
    blocking = [f for f in merged if f.get("severity") in ("CRITICAL", "HIGH")]

    pre_verify_doc = {
        "level": level,
        "effort": effort,
        "pr": summary_doc.get("pr") or {},
        "raw_count": raw_count,
        "gate_counts": counts,
        "lanes": lanes_meta,
        "shards": [m for m in raw_lanes_meta if m.get("shard")],
        "folded": folded,
        "findings": merged,
        "ac_matrix": ac_matrix,
        "high_risk": high_risk,
        "safety_alerts": safety_alerts,
        "cleared": cleared,
        "other_titles": other_titles,
    }
    write_json(os.path.join(bundle_dir, "merged-pre-verify.json"), pre_verify_doc)
    write_json(os.path.join(bundle_dir, "verify-batches.json"), verify_batches)
    write_json(os.path.join(bundle_dir, "nonblocking-candidates.json"), {
        "findings": nonblocking,
        "ac_matrix": [r for r in ac_matrix if isinstance(r, dict) and r.get("status") != "Met"],
    })

    out = {
        "level": level,
        "effort": effort,
        "raw_count": raw_count,
        "merged_count": len(merged),
        "blocking_count": len(blocking),
        "nonblocking_count": len(nonblocking),
        "verify_batch_count": len(verify_batches),
        "retry_lanes": retry_lanes,
        "gate_counts": counts,
        "high_risk": high_risk,
        "safety_alerts": safety_alerts,
        "skip_verification": len(verify_batches) == 0,
        "skip_nonblocking_eval": len(nonblocking) == 0,
        "files": {
            "pre_verify": os.path.join(bundle_dir, "merged-pre-verify.json"),
            "verify_batches": os.path.join(bundle_dir, "verify-batches.json"),
            "nonblocking_candidates": os.path.join(bundle_dir, "nonblocking-candidates.json"),
        },
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def pareto_cut(findings):
    """Apply effort/medium.md Pareto cut on todo post findings while preserving all verified CRITICAL/HIGH."""
    scored = []
    for f in findings:
        if f.get("status", "todo") != "todo":
            continue
        sw = PARETO_SEV_W.get(f.get("severity"), 1.0)
        cw = PARETO_CONF_W.get(f.get("confidence"), 0.7)
        reach = 1.5 if (
            f.get("breaking") or f.get("ac_ref") or HIGH_REACH_RX.search(str(f.get("location") or ""))
        ) else 1.0
        scored.append((round(sw * cw * reach, 3), f))
    scored.sort(key=lambda t: (-t[0], -SEV_RANK.get(t[1].get("severity"), 0)))
    total = sum(s for s, _ in scored) or 1.0

    kept, cut = [], []
    cum = 0.0
    non_blocking_count = 0
    q_count = 0
    for score, f in scored:
        is_blocking = f.get("severity") in ("CRITICAL", "HIGH") and f.get("verified") == "confirmed"
        if is_blocking:
            kept.append(f)
            cum += score
            continue
        if cum / total < 0.80:
            if f.get("severity") == "QUESTION":
                if q_count < 3:
                    kept.append(f)
                    q_count += 1
                    cum += score
                    continue
            elif non_blocking_count < 10:
                kept.append(f)
                non_blocking_count += 1
                cum += score
                continue
        cut.append(f)
    return {"shown": kept, "cut_titles": [f.get("title") for f in cut if f.get("title")], "total_post": len(scored)}


def cmd_finalize(args):
    bundle_dir = os.path.abspath(args.bundle_dir)
    pre_doc = load_json(os.path.join(bundle_dir, "merged-pre-verify.json"))
    if pre_doc is None:
        sys.stderr.write("merge_findings: merged-pre-verify.json not found; run merge first\n")
        return 2
    summary_doc = load_json(os.path.join(bundle_dir, "bundle-summary.json"), default={})
    level = str(getattr(args, "level", None) or pre_doc.get("level") or summary_doc.get("level") or "PR").upper()
    effort = args.effort or pre_doc.get("effort") or "high"
    pr_meta = summary_doc.get("pr") or pre_doc.get("pr") or {}

    v_raw = load_json(args.verifier or os.path.join(bundle_dir, "verifier-results.json"), default=[])
    v_list = v_raw.get("verdicts", v_raw) if isinstance(v_raw, dict) else (v_raw or [])
    v_by_id = {str(v.get("id")): v for v in v_list if isinstance(v, dict) and v.get("id")}

    e_raw = load_json(args.eval or os.path.join(bundle_dir, "eval-results.json"), default=[])
    e_list = e_raw.get("evaluations", e_raw) if isinstance(e_raw, dict) else (e_raw or [])
    e_by_id = {str(e.get("id")): e for e in e_list if isinstance(e, dict) and e.get("id")}

    # 1. Apply verifier verdicts
    post_verify = []
    refuted_count = 0
    for f in pre_doc.get("findings") or []:
        f = copy.deepcopy(f)
        fid = str(f.get("id") or "")
        v = v_by_id.get(fid)
        if v:
            verdict = str(v.get("verdict") or "").lower()
            if verdict == "refuted":
                refuted_count += 1
                continue
            if verdict == "unclear":
                f["verified"] = "unverified"
                f["severity"] = "QUESTION"
            elif verdict == "confirmed":
                f["verified"] = "confirmed"
                snote = str(v.get("severity_note") or "").lower()
                if snote == "severity-too-high" and f["severity"] == "HIGH":
                    f["severity"] = "MEDIUM"
                elif snote == "severity-too-low":
                    f["severity"] = SEV_UP[f["severity"]]
        elif f.get("severity") in ("CRITICAL", "HIGH") and effort in ("low", "skim"):
            f.setdefault("verified", "confirmed")
        post_verify.append(f)

    # Update AC matrix if verifiers refuted an AC
    ac_matrix = copy.deepcopy(pre_doc.get("ac_matrix") or [])
    for r in ac_matrix:
        if not isinstance(r, dict):
            continue
        v = v_by_id.get(str(r.get("ref") or ""))
        if v and str(v.get("verdict") or "").lower() == "refuted":
            r["status"] = "Met"
            r["note"] = "verifier refuted missing/contradicted claim"

    # Snapshot pre-eval candidates for breaker.py --pre safety-floor check
    write_json(os.path.join(bundle_dir, "candidates-pre-eval.json"), post_verify)

    # 2. Apply evaluation dispositions & enforce safety floor
    post_findings = []
    chat_only_findings = []
    dropped_by_eval = []
    eval_followup_needed = []
    floor_overrides = []

    for f in post_verify:
        f = copy.deepcopy(f)
        fid = str(f.get("id") or "")
        ev = e_by_id.get(fid)
        is_floor = breaker.is_floor_protected(f)
        is_confirmed_blocking = f.get("verified") == "confirmed" and f.get("severity") in ("CRITICAL", "HIGH")

        if ev:
            disp = str(ev.get("disposition") or "post")
            reason = str(ev.get("eval_reason") or "").strip() or "evaluated"
            adj = ev.get("adjusted_severity")

            if disp in ("drop", "chat-only") and is_confirmed_blocking:
                disp = "post"
                floor_overrides.append(f"{fid}: blocked evaluation {ev.get('disposition')} on confirmed {f['severity']}")

            if adj and adj in SEV_RANK and SEV_RANK[adj] < SEV_RANK[f["severity"]]:
                if is_floor:
                    floor_overrides.append(f"{fid}: blocked evaluation downgrade on floor-protected {f['severity']}")
                else:
                    f["severity"] = adj
                    f["eval_downgraded"] = True
                    f["eval_reason"] = reason

            f["eval_disposition"] = disp
            f["eval_reason"] = reason
            if disp == "drop":
                dropped_by_eval.append(f)
                continue
            if disp == "chat-only":
                chat_only_findings.append(f)
                continue
            post_findings.append(f)
        else:
            f["eval_disposition"] = "post"
            post_findings.append(f)
            # Flag non-floor confirmed HIGH or verifier-downgraded QUESTION for quick inline eval check
            if (f.get("severity") == "HIGH" and not is_floor) or (f.get("verified") == "unverified" and f.get("severity") == "QUESTION"):
                eval_followup_needed.append({
                    "id": fid, "severity": f["severity"], "lane": f.get("lane"),
                    "location": f.get("location"), "title": f.get("title"),
                })

    lanes = copy.deepcopy(pre_doc.get("lanes") or [])
    if not any(isinstance(ln, dict) and ln.get("lane") == "evaluation" for ln in lanes):
        if e_list:
            lanes.append({"lane": "evaluation", "status": "returned"})
        else:
            lanes.append({"lane": "evaluation", "status": "abstained", "reason": "no candidate findings evaluated by subagent"})

    # 3. Carry reconciliation (Gate G9) when enabled
    carry_summary = None
    prev_findings_for_breaker = None
    breaker_findings = post_findings
    prev_path = os.path.join(bundle_dir, "previous.json")
    pr_ident = (
        f"{pr_meta.get('owner', 'local')}/{pr_meta.get('repo', 'repo')}#codebase"
        if level == "CODEBASE"
        else f"{pr_meta.get('owner', 'local')}/{pr_meta.get('repo', 'repo')}#{pr_meta.get('number', 0)}"
    )
    if args.carry and os.path.isfile(prev_path):
        prev_state, _, prev_warnings = carry_findings.load_previous(prev_path)
        still_open = load_json(args.still_open, default=[]) if args.still_open else []
        if isinstance(still_open, dict):
            still_open = still_open.get("still_open") or []
        extra = {
            "pr": pr_ident,
            "last_sha": pr_meta.get("head_sha"),
            "previous": [],
        }
        out_carry, carry_summary, todo_list = carry_findings.reconcile(
            prev_state, post_findings, pre_doc.get("cleared") or [], still_open, extra
        )
        carry_summary["warnings"] = prev_warnings + carry_summary["warnings"]
        write_json(os.path.join(bundle_dir, "findings.json"), out_carry)
        write_json(os.path.join(bundle_dir, "reported.json"), todo_list)
        breaker_findings = out_carry["findings"]
        prev_findings_for_breaker = prev_state["findings"]
        # Attach cleared entries to lanes for breaker previous check
        for cl in out_carry.get("cleared") or []:
            cl_lane = cl.get("lane") or "adherence"
            matched_lane = next((ln for ln in lanes if ln.get("lane") == cl_lane), None)
            if matched_lane is None:
                matched_lane = {"lane": cl_lane, "status": "returned"}
                lanes.append(matched_lane)
            matched_lane.setdefault("cleared", []).append({"id": cl["id"], "evidence": cl["evidence"]})
    else:
        # Assign display_ids C1/H1/M1/L1/Q1 on non-carry runs
        counters = {}
        post_findings.sort(key=lambda x: -SEV_RANK.get(x.get("severity"), 0))
        for f in post_findings:
            ltr = carry_findings.SEV_LETTER.get(f["severity"], "X")
            counters[ltr] = counters.get(ltr, 0) + 1
            f["display_id"] = f"{ltr}{counters[ltr]}"
        write_json(os.path.join(bundle_dir, "findings.json"), {
            "pr": pr_ident,
            "last_sha": pr_meta.get("head_sha"),
            "findings": post_findings,
            "previous": [],
        })
        write_json(os.path.join(bundle_dir, "reported.json"), post_findings)

    # 4. Run breaker in-process
    merged_doc = {
        "level": level,
        "findings": breaker_findings,
        "ac_matrix": ac_matrix,
        "lanes": lanes,
        "folded": pre_doc.get("folded") or [],
    }
    write_json(os.path.join(bundle_dir, "merged.json"), merged_doc)
    write_json(os.path.join(bundle_dir, "breaker-input.json"), merged_doc)

    breaker_opts = {
        "level": level,
        "effort": effort,
        "hold_event": args.hold_event or ((summary_doc.get("persona") or {}).get("voice") or {}).get("hold_event") or "REQUEST_CHANGES",
        "author_is_me": bool(args.author_is_me or pr_meta.get("author_is_me")),
        "in_profile": bool(args.in_profile or (summary_doc.get("gate_g0b") or {}).get("in_skim_profile")),
        "ci": args.ci if args.ci is not None else pr_meta.get("ci"),
    }
    verdict_out = breaker.compute(merged_doc, breaker_opts, pre=post_verify, previous=prev_findings_for_breaker)

    # 5. Medium Pareto cut
    pareto_out = None
    if effort == "medium":
        pareto_src = [f for f in breaker_findings if f.get("status", "todo") == "todo"]
        pareto_out = pareto_cut(pareto_src)
        write_json(os.path.join(bundle_dir, "pareto.json"), pareto_out)

    write_json(os.path.join(bundle_dir, "chat-only.json"), chat_only_findings)
    write_json(os.path.join(bundle_dir, "eval-dropped.json"), dropped_by_eval)

    result = {
        "valid": verdict_out["valid"],
        "invalid": verdict_out["invalid"],
        "level": level,
        "effort": effort,
        "verdict": verdict_out["verdict"],
        "emoji": verdict_out["emoji"],
        "phrase": verdict_out["phrase"],
        "event": verdict_out["event"],
        "band": verdict_out["band"],
        "complete": verdict_out["complete"],
        "open": verdict_out["open"],
        "reasons": verdict_out["reasons"],
        "counts": {
            "raw": pre_doc.get("raw_count", 0),
            "post": len(post_findings),
            "chat_only": len(chat_only_findings),
            "dropped_by_eval": len(dropped_by_eval),
            "refuted": refuted_count,
            **(pre_doc.get("gate_counts") or {}),
        },
        "zero_findings_fast_path": len(post_findings) == 0 and len(chat_only_findings) == 0 and verdict_out["verdict"] in ("APPROVE", "HEALTHY"),
        "floor_overrides": floor_overrides,
        "eval_followup_needed": eval_followup_needed,
        "high_risk": pre_doc.get("high_risk") or [],
        "safety_alerts": pre_doc.get("safety_alerts") or [],
        "carry_summary": carry_summary,
        "pareto": {"shown": len(pareto_out["shown"]), "total": pareto_out["total_post"], "cut_titles": pareto_out["cut_titles"]} if pareto_out else None,
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if verdict_out["valid"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="merge_findings.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_m = sub.add_parser("merge")
    p_m.add_argument("bundle_dir")
    p_m.add_argument("--raw")
    p_m.add_argument("--effort", choices=("high", "medium", "low", "skim"))
    p_m.add_argument("--level", type=str.upper, choices=("PR", "CODEBASE"))

    p_f = sub.add_parser("finalize")
    p_f.add_argument("bundle_dir")
    p_f.add_argument("--verifier")
    p_f.add_argument("--eval")
    p_f.add_argument("--effort", choices=("high", "medium", "low", "skim"))
    p_f.add_argument("--level", type=str.upper, choices=("PR", "CODEBASE"))
    p_f.add_argument("--hold-event", dest="hold_event", choices=("REQUEST_CHANGES", "COMMENT"))
    p_f.add_argument("--author-is-me", dest="author_is_me", action="store_true")
    p_f.add_argument("--in-profile", dest="in_profile", action="store_true")
    p_f.add_argument("--ci")
    p_f.add_argument("--carry", action="store_true")
    p_f.add_argument("--still-open", dest="still_open")

    args = ap.parse_args(argv)
    if args.cmd == "merge":
        return cmd_merge(args)
    return cmd_finalize(args)


if __name__ == "__main__":
    sys.exit(main())
