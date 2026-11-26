#!/usr/bin/env python3
"""Deterministic Phase 2 merge, gates G1-G6, dedupe, verifier batching, evaluation finalize, Carry G9, Breaker, and Pareto cut.

Usage:
  merge_findings.py merge    <bundle_dir> [--raw <raw-lanes.json>] [--effort high|medium|low|skim] [--level PR|CODEBASE]
  merge_findings.py drop-g6  <bundle_dir> [--ids id1,id2] [--file <g6-drop.json>]
  merge_findings.py finalize <bundle_dir> [--verifier <verifier-results.json>] [--eval <eval-results.json>]
                             [--effort high|medium|low|skim] [--level PR|CODEBASE] [--hold-event REQUEST_CHANGES|COMMENT]
                             [--author-is-me] [--in-profile] [--ci <value>] [--carry] [--still-open <file>]
                             [--cleared <file>] [--g6-drop <ids_or_file>]

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
CONF_RANK = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}
AC_RANK = {"Contradicted": 4, "Missing": 3, "Partial": 2, "Not verifiable": 1, "Met": 0}
FLOOR_LANE_PRIORITY = {"security": 2, "adherence": 1, "business-logic": 1}

PARETO_SEV_W = {"CRITICAL": 8.0, "HIGH": 5.0, "MEDIUM": 3.0, "QUESTION": 2.0, "LOW": 1.0}
PARETO_CONF_W = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}
HIGH_REACH_RX = re.compile(r"(auth|migration|model|schema|route|api|controller|shared|core|public|payment|billing)", re.I)

FENCE_OPEN_RX = re.compile(r"(?:^|\n)[ \t]*(?:`{3,}|~{3,})[ \t]*(?:json)?[ \t]*\r?\n", re.I)
INLINE_FENCE_RX = re.compile(r"`{3,}(?:json)?\s*", re.I)
SHARD_SUFFIX_RX = re.compile(r"^(.+?)-shard-\d+$")
LOC_RX = schema_lint.LOC
HUNK_LOC_RX = schema_lint.HUNK_LOC
STOP = set(
    "the and for with that this from are not but has have was were when then than into "
    "its can will may all any one two new old use used uses get gets set sets via per should must".split()
)


class MergeInputError(Exception):
    """Raised when a required or provided JSON input file cannot be read or parsed."""


def load_json(path, default=None):
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise MergeInputError(f"cannot read JSON {path}: {exc}") from exc


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def norm_path(p, bundle_dir=None):
    p = str(p or "").strip()
    if bundle_dir:
        bd = os.path.abspath(bundle_dir).rstrip(os.sep) + os.sep
        for prefix in (bd + "head" + os.sep, bd):
            if p.startswith(prefix):
                p = p[len(prefix):]
    while True:
        if p.startswith("./"):
            p = p[2:]
        elif p.startswith("head/"):
            p = p[5:]
        else:
            break
    return p


def norm_loc(loc, bundle_dir=None):
    s = str(loc or "").strip()
    if schema_lint.INTENT_LOC.match(s):
        return s
    return norm_path(s, bundle_dir=bundle_dir)


def parse_loc(loc):
    s = norm_loc(loc)
    m = LOC_RX.match(s)
    if m:
        a = int(m.group("a"))
        b = int(m.group("b") or a)
        return norm_path(m.group("path")), a, b
    hm = HUNK_LOC_RX.match(s)
    if hm:
        a = int(hm.group("a"))
        c_raw = hm.group("count")
        count = 1 if c_raw is None else int(c_raw)
        if count == 0:
            anchor = max(1, a)
            return norm_path(hm.group("path")), anchor, anchor
        return norm_path(hm.group("path")), a, a + count - 1
    return None, None, None


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


def extract_json_from_text(text):
    """Extract JSON from raw text or fenced ```json blocks using raw_decode so nested fences inside strings work."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        pass

    dec = json.JSONDecoder()
    candidates = []
    for rx in (FENCE_OPEN_RX, INLINE_FENCE_RX):
        for m in rx.finditer(raw):
            idx = m.end()
            while idx < len(raw) and raw[idx] in " \t\r\n":
                idx += 1
            if idx < len(raw) and raw[idx] in "{[":
                try:
                    obj, _ = dec.raw_decode(raw, idx)
                    candidates.append(obj)
                except ValueError:
                    continue
        if candidates:
            break

    if candidates:
        for obj in reversed(candidates):
            if isinstance(obj, dict) and any(k in obj for k in ("findings", "lane", "status", "verdicts", "evaluations")):
                return obj
        return candidates[-1]

    # Fallback: scan for standalone JSON object/array in prose
    for i, ch in enumerate(raw):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(raw, i)
                if isinstance(obj, (dict, list)):
                    return obj
            except ValueError:
                continue
    return None


def _split_lane_hint(lane_hint):
    if not lane_hint:
        return None, None
    hint = str(lane_hint).strip()
    m = SHARD_SUFFIX_RX.match(hint)
    if m and m.group(1) in schema_lint.LANES:
        return m.group(1), hint
    return hint, None


def parse_lane_entry(item, lane_hint=None):
    """Normalize a lane output entry (JSON dict, list, or raw text containing fenced JSON)."""
    base_lane, shard_hint = _split_lane_hint(lane_hint)
    if isinstance(item, str):
        parsed = extract_json_from_text(item)
        if parsed is None:
            out = {
                "lane": base_lane or "unknown",
                "status": "NOT ASSESSED",
                "reason": "missing or unparseable JSON lane block",
                "findings": [],
                "_parse_error": True,
            }
            if shard_hint:
                out["shard"] = shard_hint
            return out
        if isinstance(parsed, list):
            if len(parsed) == 1 and isinstance(parsed[0], dict) and ("findings" in parsed[0] or "lane" in parsed[0]):
                item = parsed[0]
            elif all(isinstance(x, dict) for x in parsed):
                item = {"lane": base_lane or "unknown", "status": "returned", "findings": parsed}
            else:
                out = {
                    "lane": base_lane or "unknown",
                    "status": "NOT ASSESSED",
                    "reason": "lane output is not an object",
                    "findings": [],
                    "_parse_error": True,
                }
                if shard_hint:
                    out["shard"] = shard_hint
                return out
        else:
            item = parsed

    if not isinstance(item, dict):
        out = {
            "lane": base_lane or "unknown",
            "status": "NOT ASSESSED",
            "reason": "lane output is not an object",
            "findings": [],
            "_parse_error": True,
        }
        if shard_hint:
            out["shard"] = shard_hint
        return out

    out = dict(item)
    if not out.get("lane") and base_lane:
        out["lane"] = base_lane
    elif isinstance(out.get("lane"), str):
        ln_base, ln_shard = _split_lane_hint(out["lane"])
        if ln_shard:
            out["lane"] = ln_base
            out.setdefault("shard", ln_shard)
    if shard_hint and not out.get("shard"):
        out["shard"] = shard_hint
    if out.get("status") is None or not str(out.get("status")).strip():
        out["status"] = "returned"
    if out.get("findings") is None:
        out["findings"] = []
    return out


def load_raw_lanes(raw_obj):
    if isinstance(raw_obj, list):
        return [parse_lane_entry(x) for x in raw_obj]
    if isinstance(raw_obj, dict):
        if "lanes" in raw_obj and isinstance(raw_obj["lanes"], list):
            return [parse_lane_entry(x) for x in raw_obj["lanes"]]
        # Single-lane JSON object saved directly as raw-lanes.json
        if "findings" in raw_obj or (isinstance(raw_obj.get("lane"), str) and not isinstance(raw_obj.get(raw_obj["lane"]), dict)):
            return [parse_lane_entry(raw_obj)]
        return [parse_lane_entry(v, lane_hint=k) for k, v in raw_obj.items()]
    return []


def matches_existing_comment(f, existing):
    """Gate G5: check whether an inline comment or review body covers same file + flaw."""
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
        if cpath != fpath or not isinstance(cline, int) or isinstance(cline, bool):
            continue
        if fa - 5 <= cline <= fb + 5 and jaccard(ftext, c.get("body", "")) >= 0.35:
            return True

    reviews_list = existing.get("reviews", []) if isinstance(existing, dict) else []
    for rv in reviews_list:
        if not isinstance(rv, dict):
            continue
        body = str(rv.get("body") or "")
        if not body or fpath not in body:
            continue
        if jaccard(ftext, body) >= 0.35:
            return True
        for chunk in re.split(r"[\r\n]+|(?<=[.!?])\s+", body):
            if jaccard(ftext, chunk) >= 0.45:
                return True
    return False


def _finding_sites(f):
    out = []
    for loc in [f.get("location")] + list(f.get("locations") or []):
        p, a, b = parse_loc(loc)
        if p and a is not None:
            out.append((p, a, b))
    return out


def can_merge_findings(a, b):
    dk_a, dk_b = str(a.get("dedupe_key") or "").strip(), str(b.get("dedupe_key") or "").strip()
    if dk_a and dk_b and dk_a == dk_b:
        return True
    ta = f"{a.get('title', '')} {a.get('issue', '')}"
    tb = f"{b.get('title', '')} {b.get('issue', '')}"
    sa = _finding_sites(a)
    sb = _finding_sites(b)
    if sa and sb:
        for pa, a1, a2 in sa:
            for pb, b1, b2 in sb:
                if pa == pb and max(a1, b1) - min(a2, b2) <= 3:
                    if jaccard(ta, tb) >= 0.40:
                        return True
        return False
    if not sa and not sb:
        la, lb = norm_loc(a.get("location")), norm_loc(b.get("location"))
        if la and la == lb and schema_lint.INTENT_LOC.match(la):
            return jaccard(ta, tb) >= 0.40
    return False


def _merge_ac_refs(ref_a, ref_b):
    seen = []
    for raw in (ref_a, ref_b):
        for tok in re.split(r"[\s,;/]+", str(raw or "").strip()):
            if tok and tok not in seen:
                seen.append(tok)
    return ", ".join(seen) if seen else None


def _concrete_trigger(val):
    s = str(val or "").strip()
    return bool(s) and not s.lower().startswith("n/a")


def _f_wins(f, target):
    rf = SEV_RANK.get(f.get("severity"), -1)
    rt = SEV_RANK.get(target.get("severity"), -1)
    if rf != rt:
        return rf > rt
    return FLOOR_LANE_PRIORITY.get(f.get("lane"), 0) > FLOOR_LANE_PRIORITY.get(target.get("lane"), 0)


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

        if _f_wins(f, target):
            winner, loser = copy.deepcopy(f), copy.deepcopy(target)
        else:
            winner, loser = copy.deepcopy(target), copy.deepcopy(f)

        # Union sources
        sources = list(winner.get("sources") or [])
        for s in loser.get("sources") or []:
            if s and s not in sources:
                sources.append(s)
        winner["sources"] = sources

        # Union extra locations
        primary_loc = winner.get("location")
        extra_locs = []
        for loc in (
            list(winner.get("locations") or [])
            + [loser.get("location")]
            + list(loser.get("locations") or [])
        ):
            if loc and loc != primary_loc and loc not in extra_locs:
                extra_locs.append(loc)
        if extra_locs:
            winner["locations"] = extra_locs
        else:
            winner.pop("locations", None)

        # Preserve merged titles
        mt = []
        for t in list(winner.get("merged_titles") or []) + list(loser.get("merged_titles") or []) + [loser.get("title")]:
            if t and t != winner.get("title") and t not in mt:
                mt.append(t)
        if mt:
            winner["merged_titles"] = mt

        # Preserve carry stable id if one side holds a stable id and the other holds lane-local id
        stable_rx = carry_findings.STABLE_ID
        if not stable_rx.match(str(winner.get("id") or "")) and stable_rx.match(str(loser.get("id") or "")):
            winner["id"] = loser["id"]

        # Preserve safety-floor, contract, carry, and evidence metadata from either side
        merged_ac = _merge_ac_refs(winner.get("ac_ref"), loser.get("ac_ref"))
        if merged_ac:
            winner["ac_ref"] = merged_ac
        for opt_key in ("breaking", "code_example", "challenges", "evidence", "dedupe_key"):
            if not winner.get(opt_key) and loser.get(opt_key):
                winner[opt_key] = loser[opt_key]
        if not _concrete_trigger(winner.get("trigger")) and _concrete_trigger(loser.get("trigger")):
            winner["trigger"] = loser["trigger"]
        if CONF_RANK.get(loser.get("confidence"), -1) > CONF_RANK.get(winner.get("confidence"), -1):
            winner["confidence"] = loser["confidence"]
        if "in_diff" in winner or "in_diff" in loser:
            winner["in_diff"] = bool(winner.get("in_diff") or loser.get("in_diff"))

        target.clear()
        target.update(winner)
    return merged


def disambiguate_ids(findings):
    """Ensure every surviving finding has a unique id across shards and multi-spec lanes."""
    seen = set()
    suffix_counts = {}
    for f in findings:
        fid = str(f.get("id") or "f").strip()
        if fid not in seen:
            f["id"] = fid
            seen.add(fid)
            continue
        shard = str(f.get("shard") or "").strip()
        if shard:
            cand = f"{shard}:{fid}"
            if cand not in seen:
                f["id"] = cand
                seen.add(cand)
                continue
        n = suffix_counts.get(fid, 1) + 1
        while f"{fid}__{n}" in seen:
            n += 1
        suffix_counts[fid] = n
        cand = f"{fid}__{n}"
        f["id"] = cand
        seen.add(cand)
    return findings


def dedupe_ac_matrix(ac_rows, retry_lanes):
    """Deduplicate AC rows across sharded/multi-spec adherence lanes, keeping worst status."""
    by_ref = {}
    order = []
    for r in ac_rows:
        if not isinstance(r, dict):
            continue
        ref = str(r.get("ref") or "").strip()
        status = r.get("status")
        if not ref or status not in AC_RANK:
            retry_lanes.setdefault("adherence", []).append({
                "id": ref or None,
                "reasons": [f"G1 invalid ac_matrix row: ref={ref!r}, status={status!r}"],
            })
            continue
        key = ref.upper()
        row = copy.deepcopy(r)
        row["ref"] = ref
        if key not in by_ref:
            by_ref[key] = row
            order.append(key)
            continue
        prev = by_ref[key]
        if AC_RANK[status] > AC_RANK[prev["status"]]:
            winner, loser = row, prev
        else:
            winner, loser = prev, row
        for fld in ("evidence", "test", "note", "text"):
            wval = str(winner.get(fld) or "").strip()
            lval = str(loser.get(fld) or "").strip()
            if (not wval or wval in ("—", "untested")) and lval and lval not in ("—", "untested"):
                winner[fld] = loser[fld]
        by_ref[key] = winner
    return [by_ref[k] for k in order]


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
        ref = str(r.get("ref") or "").strip()
        p, _, _ = parse_loc(r.get("evidence"))
        key = p or "__intent__"
        by_file.setdefault(key, []).append({
            "kind": "ac",
            "id": f"ac:{ref}",
            "ref": ref,
            "location": r.get("evidence") or "intent.md",
            "claim": f"{ref} ({r.get('status')}): {r.get('text', '')}",
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
        ln, _ = _split_lane_hint(m["lane"])
        if ln not in grouped:
            grouped[ln] = []
            order.append(ln)
        grouped[ln].append(m)

    out = []
    for ln in order:
        items = grouped[ln]
        if len(items) == 1:
            one = {k: v for k, v in items[0].items() if k not in ("shard", "_parse_error")}
            one["lane"] = ln
            if one.get("status") in ("abstained", "NOT ASSESSED", "failed") and not str(one.get("reason") or "").strip():
                one["reason"] = str(one["status"])
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


def _string_list(val):
    if val is None:
        return []
    if isinstance(val, str):
        parts = [x.strip() for x in val.split(",") if x.strip()]
        return parts
    if isinstance(val, list):
        return [str(x).strip() for x in val if x is not None and str(x).strip()]
    return [str(val).strip()]


def _norm_high_risk(val):
    if val is None:
        return None
    if isinstance(val, list):
        parts = [str(x).strip() for x in val if x is not None and str(x).strip() and str(x).strip().lower() != "none"]
        return "; ".join(parts) if parts else None
    s = str(val).strip()
    if not s or s.lower() == "none":
        return None
    return s


def _apply_g3_g4_only(f, counts):
    """Apply G3/G4 checks after an open.json finding is exempted from G2 out-of-diff drop."""
    sev, conf = f.get("severity"), f.get("confidence")
    trig = str(f.get("trigger", "")).strip()
    if sev in ("CRITICAL", "HIGH") and (not trig or trig.lower().startswith("n/a")):
        if f.get("lane") == "conventions" and trig.lower().startswith("n/a (convention)"):
            return "keep"
        counts["g3_downgraded"] += 1
        f["severity"] = "QUESTION" if conf == "LOW" else "MEDIUM"
        return "downgrade"
    if sev in ("MEDIUM", "LOW") and conf == "LOW":
        counts["g4_low_conf"] += 1
        return "drop"
    return "keep"


def cmd_merge(args):
    bundle_dir = os.path.abspath(args.bundle_dir)
    raw_path = args.raw or os.path.join(bundle_dir, "raw-lanes.json")
    try:
        raw_obj = load_json(raw_path)
        if raw_obj is None:
            sys.stderr.write(f"merge_findings: raw lanes file not found: {raw_path}\n")
            return 2
        summary_doc = load_json(os.path.join(bundle_dir, "bundle-summary.json"), default={}) or {}
        level = str(getattr(args, "level", None) or summary_doc.get("level") or "PR").upper()
        if level == "CODEBASE":
            fmap_path = os.path.join(bundle_dir, "file-map.json")
            loc_map = load_json(fmap_path)
            if loc_map is None:
                sys.stderr.write(f"merge_findings: file-map.json not found for CODEBASE level: {fmap_path}\n")
                return 2
        else:
            dmap_path = os.path.join(bundle_dir, "diff-map.json")
            loc_map = load_json(dmap_path)
            if loc_map is None:
                sys.stderr.write(f"merge_findings: diff-map.json not found for PR level: {dmap_path}\n")
                return 2
        existing = load_json(os.path.join(bundle_dir, "existing-comments.json"), default={"inline": [], "reviews": []})
        open_raw = load_json(os.path.join(bundle_dir, "open.json"), default=[]) or []
    except MergeInputError as exc:
        sys.stderr.write(f"merge_findings: {exc}\n")
        return 2

    open_ids = {str(x.get("id")) for x in open_raw if isinstance(x, dict) and x.get("id")}
    effort = args.effort or (summary_doc.get("effort") or {}).get("resolved") or "high"
    folded = [x.get("lane") for x in ((summary_doc.get("lane_plan") or {}).get("folded") or []) if isinstance(x, dict) and x.get("lane")]

    lane_entries = load_raw_lanes(raw_obj)
    raw_lanes_meta = []
    retry_lanes = {}
    ac_matrix_raw = []
    high_risk = []
    safety_alerts = []
    raw_cleared = []
    other_titles = []
    surviving = []
    raw_count = 0
    counts = {
        "g2_out_of_diff": 0,
        "g3_downgraded": 0,
        "g4_low_conf": 0,
        "g5_already_raised": 0,
        "g6_linter": 0,
        "skim_dropped": 0,
    }

    for le in lane_entries:
        lname = le.get("lane") or "unknown"
        lstatus = le.get("status") or "returned"
        meta = {"lane": lname, "status": lstatus}
        if le.get("shard"):
            meta["shard"] = le["shard"]
        if le.get("reason"):
            meta["reason"] = str(le["reason"]).strip()
        hr_val = _norm_high_risk(le.get("high_risk"))
        if hr_val:
            meta["high_risk"] = hr_val
            if hr_val not in high_risk:
                high_risk.append(hr_val)
        raw_lanes_meta.append(meta)

        for sa in _string_list(le.get("safety_alerts")):
            if sa and sa.lower() not in ("none", "none observed") and sa not in safety_alerts:
                safety_alerts.append(sa)
        for c in le.get("cleared") or []:
            if isinstance(c, dict) and c.get("id") and str(c.get("evidence") or "").strip():
                cc = dict(c)
                cc.setdefault("lane", lname)
                raw_cleared.append(cc)
        for ot in _string_list(le.get("other")):
            if ot and ot.lower() not in ("none", "none observed"):
                other_titles.append(ot)
        if lname in ("adherence", "business-logic", "critical-risk"):
            for r in le.get("ac_matrix") or []:
                if isinstance(r, dict):
                    ac_matrix_raw.append(r)

        if lstatus in ("abstained", "NOT ASSESSED", "failed"):
            if le.get("_parse_error"):
                retry_key = le.get("shard") or lname
                retry_lanes.setdefault(retry_key, []).append({"id": None, "reasons": [le.get("reason") or "unparseable lane JSON"]})
            continue

        findings_list = le.get("findings")
        if not isinstance(findings_list, list):
            retry_key = le.get("shard") or lname
            retry_lanes.setdefault(retry_key, []).append({"id": None, "reasons": ["G1 findings must be a JSON array"]})
            continue

        for f in findings_list:
            if not isinstance(f, dict):
                continue
            raw_count += 1
            f = copy.deepcopy(f)
            f.setdefault("lane", lname)
            if le.get("shard") and not f.get("shard"):
                f["shard"] = le["shard"]
            if f.get("location"):
                f["location"] = norm_loc(f["location"], bundle_dir=bundle_dir)
            if isinstance(f.get("locations"), list):
                f["locations"] = [norm_loc(x, bundle_dir=bundle_dir) for x in f["locations"]]

            action, reasons, kept_locs = schema_lint.check_detail(f, loc_map, level=level)
            if action == "retry_lane":
                retry_key = le.get("shard") or lname
                retry_lanes.setdefault(retry_key, []).append({"id": f.get("id"), "reasons": reasons})
                continue

            in_diff_flag = not bool(schema_lint.INTENT_LOC.match(str(f.get("location", ""))))
            if action == "drop":
                if (
                    level == "PR"
                    and str(f.get("id") or "") in open_ids
                    and all(r.startswith("G2") for r in reasons)
                    and f.get("introduced_by_pr") != "pre-existing"
                ):
                    in_diff_flag = False
                    action = _apply_g3_g4_only(f, counts)
                    if action == "drop":
                        continue
                else:
                    if any(r.startswith("G2") for r in reasons):
                        counts["g2_out_of_diff"] += 1
                    else:
                        counts["g4_low_conf"] += 1
                    continue
            else:
                if kept_locs and str(f.get("id") or "") not in open_ids:
                    f["location"] = kept_locs[0]
                    if len(kept_locs) > 1:
                        f["locations"] = kept_locs[1:]
                    else:
                        f.pop("locations", None)
                    in_diff_flag = not bool(schema_lint.INTENT_LOC.match(str(f.get("location", ""))))
                if action == "downgrade":
                    counts["g3_downgraded"] += 1
                    f["severity"] = "QUESTION" if f.get("confidence") == "LOW" else "MEDIUM"

            f["in_diff"] = in_diff_flag

            # Gate G5 (already raised — PR mode only)
            if level == "PR" and matches_existing_comment(f, existing):
                counts["g5_already_raised"] += 1
                continue
            surviving.append(f)

    # Rules-changed automatic high_risk (PR mode only)
    if level == "PR" and summary_doc.get("rules_changed") and "rule files edited" not in high_risk:
        high_risk.append("rule files edited")

    lanes_meta = aggregate_lanes_meta(raw_lanes_meta)
    merged = disambiguate_ids(dedupe_findings(surviving))
    ac_matrix = dedupe_ac_matrix(ac_matrix_raw, retry_lanes)
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
        "merged_count": len(merged),
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


def _parse_drop_ids_spec(spec_str):
    if not spec_str:
        return set()
    if os.path.isfile(spec_str):
        doc = load_json(spec_str, default=[])
        if isinstance(doc, dict):
            doc = doc.get("drop") or doc.get("ids") or doc.get("findings") or []
        out = set()
        for item in doc if isinstance(doc, list) else []:
            if isinstance(item, dict) and item.get("id"):
                out.add(str(item["id"]))
            elif isinstance(item, str) and item.strip():
                out.add(item.strip())
        return out
    return {x.strip() for x in str(spec_str).split(",") if x.strip()}


def _apply_g6_drop(findings, drop_ids):
    if not drop_ids:
        return list(findings), [], []
    kept, dropped, skipped_blocking = [], [], []
    for f in findings:
        fid = str(f.get("id") or "")
        if fid in drop_ids:
            if f.get("severity") in ("CRITICAL", "HIGH"):
                skipped_blocking.append(fid)
                kept.append(f)
            else:
                dropped.append(fid)
        else:
            kept.append(f)
    return kept, dropped, skipped_blocking


def cmd_drop_g6(args):
    bundle_dir = os.path.abspath(args.bundle_dir)
    pre_path = os.path.join(bundle_dir, "merged-pre-verify.json")
    try:
        pre_doc = load_json(pre_path)
        if pre_doc is None:
            sys.stderr.write("merge_findings: merged-pre-verify.json not found; run merge first\n")
            return 2
        drop_ids = set()
        if args.ids:
            drop_ids |= _parse_drop_ids_spec(args.ids)
        if args.file:
            drop_ids |= _parse_drop_ids_spec(args.file)
    except MergeInputError as exc:
        sys.stderr.write(f"merge_findings: {exc}\n")
        return 2

    kept, dropped, skipped_blocking = _apply_g6_drop(pre_doc.get("findings") or [], drop_ids)
    counts = dict(pre_doc.get("gate_counts") or {})
    counts["g6_linter"] = counts.get("g6_linter", 0) + len(dropped)
    pre_doc["findings"] = kept
    pre_doc["gate_counts"] = counts
    write_json(pre_path, pre_doc)

    nb_path = os.path.join(bundle_dir, "nonblocking-candidates.json")
    nb_doc = load_json(nb_path, default={}) if os.path.isfile(nb_path) else None
    if isinstance(nb_doc, dict):
        nb_doc["findings"] = [f for f in kept if f.get("severity") in ("MEDIUM", "LOW", "QUESTION")]
        write_json(nb_path, nb_doc)

    out = {"dropped": dropped, "skipped_blocking": skipped_blocking, "gate_counts": counts}
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _emphasis_rank(f, emphasis_list):
    if not emphasis_list:
        return 0
    lane = str(f.get("lane") or "").lower()
    srcs = [str(s).lower() for s in (f.get("sources") or [])]
    dim = str(f.get("dimension") or "").lower()
    for idx, emp in enumerate(emphasis_list):
        e = str(emp).strip().lower()
        if not e:
            continue
        norm_e = "compatibility" if e == "backward-compatibility" else e
        if lane == norm_e or norm_e in srcs or e in dim or norm_e in dim:
            return idx
    return len(emphasis_list)


def pareto_cut(findings, emphasis=None):
    """Apply effort/medium.md Pareto cut on todo post findings while preserving all verified CRITICAL/HIGH."""
    emphasis_list = list(emphasis or [])
    scored = []
    for idx, f in enumerate(findings):
        if f.get("status", "todo") != "todo":
            continue
        sw = PARETO_SEV_W.get(f.get("severity"), 1.0)
        cw = PARETO_CONF_W.get(f.get("confidence"), 0.7)
        loc_blob = " ".join([str(f.get("location") or "")] + [str(x) for x in (f.get("locations") or [])])
        reach = 1.5 if (
            f.get("breaking") or f.get("ac_ref") or HIGH_REACH_RX.search(loc_blob)
        ) else 1.0
        scored.append((round(sw * cw * reach, 3), idx, f))
    scored.sort(key=lambda t: (-t[0], -SEV_RANK.get(t[2].get("severity"), 0), _emphasis_rank(t[2], emphasis_list), t[1]))
    total = sum(s for s, _, _ in scored) or 1.0

    kept, cut = [], []
    cum = 0.0
    non_blocking_count = 0
    q_count = 0
    for score, _idx, f in scored:
        is_blocking = f.get("severity") in ("CRITICAL", "HIGH")  # plausible and unverified items also stay visible
        if is_blocking:
            kept.append(f)
            cum += score
            continue
        if (cum / total) < (0.80 - 1e-9):
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


def _extract_result_items(raw_obj, list_key):
    """Accept bare list, {list_key: [...]}, or fenced JSON batch outputs."""
    if raw_obj is None:
        return []
    if isinstance(raw_obj, dict):
        items = raw_obj.get(list_key, [])
        return items if isinstance(items, list) else []
    if not isinstance(raw_obj, list):
        return []
    out = []
    for entry in raw_obj:
        if isinstance(entry, str):
            parsed = extract_json_from_text(entry)
            if isinstance(parsed, dict) and isinstance(parsed.get(list_key), list):
                out.extend(parsed[list_key])
            elif isinstance(parsed, list):
                out.extend(parsed)
        elif isinstance(entry, dict) and list_key in entry and isinstance(entry[list_key], list):
            out.extend(entry[list_key])
        elif isinstance(entry, dict):
            out.append(entry)
    return out


def _redact_finding(f):
    """Scrub emails and @handles from durable finding prose fields before persistence."""
    out = copy.deepcopy(f)
    for k in ("title", "issue", "trigger", "impact", "fix", "evidence", "reason", "challenge", "eval_reason"):
        if isinstance(out.get(k), str) and out[k]:
            out[k] = carry_findings.redact(out[k], handles=True)
    if isinstance(out.get("code_example"), str) and out["code_example"]:
        out["code_example"] = carry_findings.redact(out["code_example"], handles=False)
    return out


def _ac_refs_of(f):
    return {t.upper() for t in re.split(r"[\s,;/()]+", str(f.get("ac_ref") or "")) if t}


def cmd_finalize(args):
    bundle_dir = os.path.abspath(args.bundle_dir)
    try:
        pre_doc = load_json(os.path.join(bundle_dir, "merged-pre-verify.json"))
        if pre_doc is None:
            sys.stderr.write("merge_findings: merged-pre-verify.json not found; run merge first\n")
            return 2
        summary_doc = load_json(os.path.join(bundle_dir, "bundle-summary.json"), default={}) or {}
        for flag_name, flag_val in (
            ("--verifier", args.verifier),
            ("--eval", args.eval),
            ("--cleared", args.cleared),
            ("--still-open", args.still_open),
        ):
            if flag_val and not os.path.isfile(flag_val):
                sys.stderr.write(f"merge_findings: {flag_name} file not found: {flag_val}\n")
                return 2
        v_raw = load_json(args.verifier or os.path.join(bundle_dir, "verifier-results.json"), default=[])
        e_raw = load_json(args.eval or os.path.join(bundle_dir, "eval-results.json"), default=[])
        if args.g6_drop and args.g6_drop.endswith(".json") and not os.path.isfile(args.g6_drop):
            sys.stderr.write(f"merge_findings: --g6-drop file not found: {args.g6_drop}\n")
            return 2
        g6_drop_ids = _parse_drop_ids_spec(args.g6_drop) if args.g6_drop else set()
        cleared_arg_doc = load_json(args.cleared, default=[]) if args.cleared else []
        still_arg_doc = load_json(args.still_open, default=[]) if args.still_open else []
    except MergeInputError as exc:
        sys.stderr.write(f"merge_findings: {exc}\n")
        return 2

    level = str(getattr(args, "level", None) or pre_doc.get("level") or summary_doc.get("level") or "PR").upper()
    effort = args.effort or pre_doc.get("effort") or "high"
    pr_meta = summary_doc.get("pr") or pre_doc.get("pr") or {}

    base_findings = list(pre_doc.get("findings") or [])
    g6_added = 0
    if g6_drop_ids:
        base_findings, g6_dropped, _ = _apply_g6_drop(base_findings, g6_drop_ids)
        g6_added = len(g6_dropped)

    v_list = _extract_result_items(v_raw, "verdicts")
    v_finding_by_id = {}
    v_ac_by_ref = {}
    for v in v_list:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id") or "").strip()
        if v.get("kind") == "ac" or vid.startswith("ac:"):
            ref = str(v.get("ref") or (vid[3:] if vid.startswith("ac:") else vid)).strip()
            if ref:
                v_ac_by_ref[ref.upper()] = v
        elif vid:
            v_finding_by_id[vid] = v

    e_list = _extract_result_items(e_raw, "evaluations")
    e_by_id = {str(e.get("id")): e for e in e_list if isinstance(e, dict) and e.get("id")}

    # 1. Apply verifier verdicts
    post_verify = []
    verifier_downgraded_ids = set()
    refuted_count = 0
    finding_ids = {str(f.get("id") or "") for f in base_findings}
    for f in base_findings:
        f = copy.deepcopy(f)
        fid = str(f.get("id") or "")
        v = v_finding_by_id.get(fid)
        if v:
            verdict = str(v.get("verdict") or "").lower()
            if verdict == "refuted":
                refuted_count += 1
                continue
            if verdict == "unclear":
                f["verified"] = "unverified"
                f["severity"] = "QUESTION"
            elif verdict == "plausible":
                # Not refuted, but the trigger depends on input, config or state that the code cannot show.
                # The severity stays. The finding has no safety floor.
                f["verified"] = "plausible"
            elif verdict == "confirmed":
                f["verified"] = "confirmed"
                snote = str(v.get("severity_note") or "").lower()
                if snote == "severity-too-high" and f["severity"] in ("CRITICAL", "HIGH"):
                    old_sev = f["severity"]
                    f["severity"] = SEV_DOWN[old_sev]
                    if old_sev == "HIGH" and f["severity"] == "MEDIUM":
                        if f.get("confidence") == "LOW":
                            f["severity"] = "QUESTION"
                        verifier_downgraded_ids.add(fid)
                elif snote == "severity-too-low":
                    f["severity"] = SEV_UP[f["severity"]]
        elif f.get("severity") in ("CRITICAL", "HIGH"):
            # No verifier result at any effort: the finding was not checked. It still counts for the
            # verdict, and it has no safety floor.
            f.setdefault("verified", "unverified")
        post_verify.append(f)

    # Update AC matrix if verifiers refuted an AC
    ac_matrix = copy.deepcopy(pre_doc.get("ac_matrix") or [])
    for r in ac_matrix:
        if not isinstance(r, dict):
            continue
        ref = str(r.get("ref") or "").strip()
        v = v_ac_by_ref.get(ref.upper())
        if v is None and ref and ref not in finding_ids:
            v = v_finding_by_id.get(ref)
        if v and str(v.get("verdict") or "").lower() == "refuted":
            r["status"] = "Met"
            r["note"] = "verifier refuted missing/contradicted claim"

    # Keep nonblocking-candidates.json updated if verifier downgraded HIGH -> MEDIUM/QUESTION
    if verifier_downgraded_ids:
        nb_path = os.path.join(bundle_dir, "nonblocking-candidates.json")
        nb_existing = load_json(nb_path, default={}) if os.path.isfile(nb_path) else {}
        if not isinstance(nb_existing, dict):
            nb_existing = {}
        nb_findings = [copy.deepcopy(f) for f in post_verify if f.get("severity") in ("MEDIUM", "LOW", "QUESTION")]
        write_json(nb_path, {
            "findings": nb_findings,
            "ac_matrix": [r for r in ac_matrix if isinstance(r, dict) and r.get("status") != "Met"],
        })

    # Snapshot pre-eval candidates (redacted) for breaker.py --pre safety-floor check
    post_verify = [_redact_finding(x) for x in post_verify]
    write_json(os.path.join(bundle_dir, "candidates-pre-eval.json"), post_verify)

    # Track unsatisfied AC refs so evaluation cannot orphan a Missing/Contradicted AC
    unsat_refs = {
        str(r.get("ref") or "").upper()
        for r in ac_matrix
        if isinstance(r, dict) and r.get("status") in ("Missing", "Contradicted") and r.get("ref")
    }
    ac_ref_counts = {}
    for f in post_verify:
        for ref in _ac_refs_of(f) & unsat_refs:
            ac_ref_counts[ref] = ac_ref_counts.get(ref, 0) + 1

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
        is_blocking_sev = f.get("severity") in ("CRITICAL", "HIGH")

        if ev:
            disp = str(ev.get("disposition") or "post").strip().lower()
            reason = str(ev.get("eval_reason") or "").strip() or "evaluated"
            adj = ev.get("adjusted_severity")

            if disp in ("drop", "chat-only") and is_blocking_sev:
                disp = "post"
                label = "confirmed" if is_confirmed_blocking else str(f.get("verified") or "unverified")
                floor_overrides.append(f"{fid}: blocked evaluation {ev.get('disposition')} on {label} {f['severity']}")
            elif disp in ("drop", "chat-only"):
                sole_ac = [ref for ref in (_ac_refs_of(f) & unsat_refs) if ac_ref_counts.get(ref, 0) <= 1]
                if sole_ac:
                    disp = "post"
                    floor_overrides.append(f"{fid}: blocked evaluation {ev.get('disposition')} on sole finding for AC {', '.join(sorted(sole_ac))}")

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
                for ref in (_ac_refs_of(f) & unsat_refs):
                    ac_ref_counts[ref] = max(0, ac_ref_counts.get(ref, 1) - 1)
                dropped_by_eval.append(f)
                continue
            if disp == "chat-only":
                for ref in (_ac_refs_of(f) & unsat_refs):
                    ac_ref_counts[ref] = max(0, ac_ref_counts.get(ref, 1) - 1)
                chat_only_findings.append(f)
                continue
            post_findings.append(f)
        else:
            f["eval_disposition"] = "post"
            post_findings.append(f)
            if (
                (f.get("severity") == "HIGH" and not is_floor)
                or (f.get("severity") in ("CRITICAL", "HIGH") and f.get("verified") in ("plausible", "unverified"))
                or (f.get("verified") == "unverified" and f.get("severity") == "QUESTION")
                or (fid in verifier_downgraded_ids)
            ):
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

    lead_cleared = []
    explicit_prev_outcomes = None
    if isinstance(cleared_arg_doc, dict):
        lead_cleared = [c for c in (cleared_arg_doc.get("cleared") or []) if isinstance(c, dict)]
        if isinstance(cleared_arg_doc.get("previous"), list):
            explicit_prev_outcomes = cleared_arg_doc["previous"]
    elif isinstance(cleared_arg_doc, list):
        lead_cleared = [c for c in cleared_arg_doc if isinstance(c, dict)]

    still_open = []
    if isinstance(still_arg_doc, dict):
        still_open = [str(s) for s in (still_arg_doc.get("still_open") or []) if s]
        if isinstance(still_arg_doc.get("cleared"), list):
            lead_cleared.extend([c for c in still_arg_doc["cleared"] if isinstance(c, dict)])
        if explicit_prev_outcomes is None and isinstance(still_arg_doc.get("previous"), list):
            explicit_prev_outcomes = still_arg_doc["previous"]
    elif isinstance(still_arg_doc, list):
        still_open = [str(s) for s in still_arg_doc if s]

    combined_cleared = list(pre_doc.get("cleared") or []) + lead_cleared
    resolved_cleared_ids = {
        str(c.get("id")) for c in combined_cleared if isinstance(c, dict) and c.get("resolved") is True and c.get("id")
    }

    if args.carry:
        try:
            if os.path.isfile(prev_path):
                prev_state, _, prev_warnings = carry_findings.load_previous(prev_path)
            else:
                prev_state, _, prev_warnings = carry_findings.load_previous("-")
        except carry_findings.BadInput as exc:
            sys.stderr.write(f"merge_findings: {exc}\n")
            return 2

        carry_in = []
        for f in post_findings:
            cf = copy.deepcopy(f)
            cf.setdefault("origin", "new")
            cf.setdefault("status", "todo")
            carry_in.append(cf)

        if explicit_prev_outcomes is not None:
            prev_outcomes = explicit_prev_outcomes
        elif prev_state["findings"]:
            prev_outcomes = [
                {
                    "id": pf["id"],
                    "outcome": "resolved" if pf["id"] in resolved_cleared_ids else "still open",
                }
                for pf in prev_state["findings"]
                if isinstance(pf, dict) and pf.get("status", "todo") == "todo" and pf.get("id")
            ]
        else:
            prev_outcomes = []

        extra = {
            "pr": pr_ident,
            "last_sha": pr_meta.get("head_sha"),
            "previous": prev_outcomes,
        }
        try:
            out_carry, carry_summary, todo_list = carry_findings.reconcile(
                prev_state, carry_in, combined_cleared, still_open, extra
            )
        except carry_findings.BadInput as exc:
            sys.stderr.write(f"merge_findings: {exc}\n")
            return 2

        out_carry["findings"] = [_redact_finding(x) for x in out_carry["findings"]]
        todo_list = [_redact_finding(x) for x in todo_list]
        carry_summary["warnings"] = prev_warnings + carry_summary["warnings"]
        write_json(os.path.join(bundle_dir, "findings.json"), out_carry)
        write_json(os.path.join(bundle_dir, "reported.json"), todo_list)
        breaker_findings = out_carry["findings"]
        prev_findings_for_breaker = prev_state["findings"] if prev_state["findings"] else None

        # Attach non-resolved cleared entries to valid breaker lanes for check_previous
        for cl in out_carry.get("cleared") or []:
            if cl.get("resolved") or cl.get("id") in resolved_cleared_ids:
                continue
            raw_lane = cl.get("lane")
            cl_lane = raw_lane if raw_lane in schema_lint.LANES else "adherence"
            matched_lane = next((ln for ln in lanes if ln.get("lane") == cl_lane), None)
            if matched_lane is None:
                matched_lane = {"lane": cl_lane, "status": "returned"}
                lanes.append(matched_lane)
            matched_lane.setdefault("cleared", []).append({"id": cl["id"], "evidence": cl["evidence"]})
    else:
        # Assign display_ids C1/H1/M1/L1/Q1, origin, status, and redact PII on non-carry runs
        counters = {}
        post_findings.sort(key=lambda x: (-SEV_RANK.get(x.get("severity"), 0), str(x.get("id") or "")))
        redacted_post = []
        for f in post_findings:
            rf = _redact_finding(f)
            rf.setdefault("origin", "new")
            rf.setdefault("status", "todo")
            ltr = carry_findings.SEV_LETTER.get(rf["severity"], "X")
            counters[ltr] = counters.get(ltr, 0) + 1
            rf["display_id"] = f"{ltr}{counters[ltr]}"
            redacted_post.append(rf)
        post_findings = redacted_post
        breaker_findings = post_findings
        write_json(os.path.join(bundle_dir, "findings.json"), {
            "pr": pr_ident,
            "last_sha": pr_meta.get("head_sha"),
            "findings": post_findings,
            "previous": [],
        })
        write_json(os.path.join(bundle_dir, "reported.json"), post_findings)

    # Ensure lane entries have valid names and reasons before breaker
    sanitized_lanes = []
    for ln in lanes:
        if not isinstance(ln, dict):
            continue
        item = dict(ln)
        if item.get("lane") not in schema_lint.LANES:
            item["lane"] = "adherence"
        if item.get("status") in ("abstained", "NOT ASSESSED", "failed") and not str(item.get("reason") or "").strip():
            item["reason"] = str(item["status"])
        sanitized_lanes.append(item)

    # 4. Run breaker in-process
    breaker_opts = {
        "level": level,
        "effort": effort,
        "hold_event": args.hold_event or ((summary_doc.get("persona") or {}).get("voice") or {}).get("hold_event") or "REQUEST_CHANGES",
        "author_is_me": bool(args.author_is_me or pr_meta.get("author_is_me")),
        "in_profile": bool(args.in_profile or (summary_doc.get("gate_g0b") or {}).get("in_skim_profile")),
        "ci": args.ci if args.ci is not None else pr_meta.get("ci"),
    }
    merged_doc = {
        "level": level,
        "effort": breaker_opts["effort"],
        "hold_event": breaker_opts["hold_event"],
        "author_is_me": breaker_opts["author_is_me"],
        "in_profile": breaker_opts["in_profile"],
        "findings": breaker_findings,
        "ac_matrix": ac_matrix,
        "lanes": sanitized_lanes,
        "folded": pre_doc.get("folded") or [],
    }
    if breaker_opts["ci"] is not None:
        merged_doc["ci"] = breaker_opts["ci"]
    write_json(os.path.join(bundle_dir, "merged.json"), merged_doc)
    write_json(os.path.join(bundle_dir, "breaker-input.json"), merged_doc)

    verdict_out = breaker.compute(merged_doc, breaker_opts, pre=post_verify, previous=prev_findings_for_breaker)

    # 5. Medium Pareto cut
    pareto_out = None
    if effort == "medium":
        pareto_src = [f for f in breaker_findings if f.get("status", "todo") == "todo"]
        emphasis = (summary_doc.get("persona") or {}).get("emphasis") or []
        pareto_out = pareto_cut(pareto_src, emphasis=emphasis)
        write_json(os.path.join(bundle_dir, "pareto.json"), pareto_out)

    write_json(os.path.join(bundle_dir, "chat-only.json"), [_redact_finding(x) for x in chat_only_findings])
    write_json(os.path.join(bundle_dir, "eval-dropped.json"), [_redact_finding(x) for x in dropped_by_eval])

    gate_counts = dict(pre_doc.get("gate_counts") or {})
    gate_counts.setdefault("g6_linter", 0)
    gate_counts["g6_linter"] += g6_added
    merged_total = pre_doc.get("merged_count")
    if merged_total is None:
        merged_total = len(pre_doc.get("findings") or [])

    open_breaker = [
        f for f in breaker_findings
        if isinstance(f, dict)
        and f.get("status", "todo") == "todo"
        and f.get("eval_disposition") not in ("drop", "chat-only")
    ]
    zero_fast_path = (
        len(open_breaker) == 0
        and len(chat_only_findings) == 0
        and not bool(pre_doc.get("safety_alerts"))
        and not bool(carry_summary and carry_summary.get("declined_still_present"))
        and bool(verdict_out.get("valid"))
        and verdict_out.get("verdict") in ("APPROVE", "HEALTHY")
    )

    counts_out = {
        "raw": pre_doc.get("raw_count", 0),
        "merged": merged_total,
        "post": len(post_findings),
        "chat_only": len(chat_only_findings),
        "dropped_by_eval": len(dropped_by_eval),
        "refuted": refuted_count,
        **gate_counts,
    }
    if pareto_out is not None:
        counts_out["pareto_shown"] = len(pareto_out["shown"])
        counts_out["pareto_cut"] = len(pareto_out["cut_titles"])

    result = {
        "valid": verdict_out["valid"],
        "invalid": verdict_out["invalid"],
        "level": level,
        "effort": effort,
        "verdict": verdict_out["verdict"],
        "emoji": verdict_out["emoji"],
        "phrase": verdict_out["phrase"],
        "event": verdict_out["event"],
        "next_step": verdict_out.get("next_step"),
        "band": verdict_out["band"],
        "complete": verdict_out["complete"],
        "open": verdict_out["open"],
        "reasons": verdict_out["reasons"],
        "counts": counts_out,
        "zero_findings_fast_path": zero_fast_path,
        "floor_overrides": floor_overrides,
        "eval_followup_needed": eval_followup_needed,
        "high_risk": pre_doc.get("high_risk") or [],
        "safety_alerts": pre_doc.get("safety_alerts") or [],
        "carry_summary": carry_summary,
        "pareto": {"shown": len(pareto_out["shown"]), "total": pareto_out["total_post"], "cut_titles": pareto_out["cut_titles"]} if pareto_out else None,
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    for line in verdict_out.get("invalid") or []:
        sys.stderr.write(f"INVALID: {line}\n")
    return 0 if verdict_out["valid"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="merge_findings.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_m = sub.add_parser("merge")
    p_m.add_argument("bundle_dir")
    p_m.add_argument("--raw")
    p_m.add_argument("--effort", choices=("high", "medium", "low", "skim"))
    p_m.add_argument("--level", type=str.upper, choices=("PR", "CODEBASE"))

    p_g6 = sub.add_parser("drop-g6")
    p_g6.add_argument("bundle_dir")
    p_g6.add_argument("--ids")
    p_g6.add_argument("--file")

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
    p_f.add_argument("--cleared", dest="cleared")
    p_f.add_argument("--g6-drop", dest="g6_drop")

    args = ap.parse_args(argv)
    if args.cmd == "merge":
        return cmd_merge(args)
    if args.cmd == "drop-g6":
        return cmd_drop_g6(args)
    return cmd_finalize(args)


if __name__ == "__main__":
    sys.exit(main())
