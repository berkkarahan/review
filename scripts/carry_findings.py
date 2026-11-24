#!/usr/bin/env python3
"""Carry finding state between two reviews of the same PR (stable ids, declines, open findings).

Usage:
  carry_findings.py prepare   <prev_findings.json|-> <bundle_dir> [--level PR|CODEBASE]
  carry_findings.py reconcile <prev_findings.json|-> <new_findings.json> <out.json> [--reported <file>] [--level PR|CODEBASE]
  carry_findings.py mark      <findings.json> <decisions.json> [--level PR|CODEBASE]

prepare    Reads the previous findings.json. Writes <bundle_dir>/previous.json (the working
           copy, ids normalised), <bundle_dir>/declines.json and <bundle_dir>/open.json.
           Prints a JSON summary with next_id. Use "-" when there is no previous review:
           it writes nothing and prints an empty summary.
reconcile  Applies the carry rules to the new findings and writes out.json (the next
           findings.json). Assigns stable ids. Prints a JSON summary. --reported writes the
           todo findings only (the ones the report shows) to a second file.
mark       Sets status on findings in an existing findings.json, in place.
           decisions.json is a list of {"id", "status": "todo|fixed|declined", "commit"?, "reason"?}.

new_findings.json is a JSON array of findings, or an object:
  {"pr"?, "last_sha"?, "previous"?, "findings": [...],
   "cleared": [{"id", "evidence", "lane"?, "resolved"?: true}],
   "still_open": ["<id>", ...]}

Exit code 0 on success. A non-zero exit and a message on stderr mean bad input.
Python standard library only. The script writes no names. It redacts emails and @handles
in durable finding strings, notes, warnings, declines.json, and open.json.
"""
import json
import os
import re
import sys

SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "QUESTION": 4}
SEV_LETTER = {"CRITICAL": "C", "HIGH": "H", "MEDIUM": "M", "LOW": "L", "QUESTION": "Q"}
BLOCKING = ("CRITICAL", "HIGH")
STATUSES = ("todo", "fixed", "declined")
REVIEW_LEVELS = ("PR", "CODEBASE")

# Lane letters. None is C, H, M, L or Q, so a stable id never reads as a report label.
LANE_LETTER = {
    "adherence": "B", "business-logic": "B", "code-quality": "D", "security": "S", "performance": "P",
    "compatibility": "A", "robustness": "R", "conventions": "V", "skim": "K",
}
OTHER_LETTER = "X"  # critical-risk, unknown or missing lane
VALID_LETTERS = set(LANE_LETTER.values()) | {OTHER_LETTER}
VALID_LANES = set(LANE_LETTER.keys()) | {"critical-risk", "evaluation"}
STABLE_ID = re.compile(r"^([BDSPARVKX])(\d+)$")

NEAR_LINES = 5          # same flaw sits within 5 lines (the G5 distance)
SIM_NEAR = 0.30         # word overlap needed when the sites are near
SIM_NEAR_DECLINE = 0.45  # stricter when the match would drop a finding
SIM_FAR = 0.60          # word overlap needed when the sites are not near
AMBIGUOUS = 0.05        # two candidates this close: not certain, so the finding is new

LOC = re.compile(r"^(?P<path>[^\n:]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
HUNK_LOC = re.compile(r"^(?P<path>.+?)\s+@@\s+-\d+(?:,\d+)?\s+\+(?P<a>\d+)(?:,(?P<count>\d+))?\s+@@")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HANDLE = re.compile(
    r"(?<![\w.@/])@[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,37}[A-Za-z0-9_-])?"
    r"(?:/[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,37}[A-Za-z0-9_-])?)?(?:\[bot\])?(?!\w|\.\w|[(/])"
)
PROSE_FIELDS = ("title", "issue", "problem", "fix", "trigger", "impact",
                "evidence", "reason", "challenge", "eval_reason")
STOP = set("the and for with that this from are not but has have was were when then than into "
           "its can will may all any one two new old use used uses get gets set sets via per".split())


class BadInput(Exception):
    pass


def die(msg):
    sys.stderr.write("carry_findings: error: " + msg + "\n")
    sys.exit(2)


# ---------------------------------------------------------------- helpers

def sstr(v):
    return "" if v is None else str(v)


def load_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise BadInput(f"{what} not found: {path}")
    except (ValueError, OSError) as exc:
        raise BadInput(f"{what} is not valid JSON ({path}): {exc}")


def write_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise BadInput(f"cannot write {path}: {exc}")


def redact(text, handles=False):
    text = EMAIL.sub("<email>", sstr(text))
    if handles:
        text = HANDLE.sub("<handle>", text)
    return text


def redact_finding(f):
    out = dict(f)
    for k in PROSE_FIELDS:
        if isinstance(out.get(k), str):
            out[k] = redact(out[k], handles=True)
    if isinstance(out.get("code_example"), str):
        out["code_example"] = redact(out["code_example"], handles=False)
    if isinstance(out.get("merged_titles"), list):
        out["merged_titles"] = [redact(t, handles=True) if isinstance(t, str) else t for t in out["merged_titles"]]
    return out


def norm_path(p):
    p = sstr(p).strip()
    changed = True
    while changed:
        changed = False
        for prefix in ("head/", "./"):
            if p.startswith(prefix):
                p = p[len(prefix):]
                changed = True
    return p


def parse_lines(text):
    if text is None:
        return None
    m = re.match(r"^(\d+)(?:-(\d+))?$", str(text).strip())
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2) or a)
    if a < 1 or b < a:
        return None
    return (a, b)


def _parse_site(loc):
    loc = sstr(loc).strip()
    if not loc:
        return None
    m = LOC.match(loc)
    if m:
        a = int(m.group("a"))
        b = int(m.group("b") or a)
        return (norm_path(m.group("path")), (a, b) if a >= 1 and b >= a else None)
    hm = HUNK_LOC.match(loc)
    if hm:
        a = int(hm.group("a"))
        count = int(hm.group("count") or 1)
        b = a if count == 0 else a + count - 1
        return (norm_path(hm.group("path")), (a, b))
    first = loc.split()[0] if loc.split() else ""
    if "/" in first or "." in first:
        return (norm_path(first), None)
    return (loc, None)


def sites(f):
    """List of (path, (a, b) | None) for location, locations and file/lines."""
    out = []
    if isinstance(f.get("file"), str) and f["file"].strip():
        out.append((norm_path(f["file"]), parse_lines(f.get("lines"))))
    extra = f.get("locations")
    loc_items = [f.get("location")] + (list(extra) if isinstance(extra, list) else [])
    for loc in loc_items:
        if not isinstance(loc, str) or not loc.strip():
            continue
        parsed = _parse_site(loc)
        if parsed is not None:
            out.append(parsed)
    return out


def file_and_lines(f):
    s = sites(f)
    if not s:
        return "", ""
    path, rng = s[0]
    if rng is None:
        return path, ""
    return path, (str(rng[0]) if rng[0] == rng[1] else f"{rng[0]}-{rng[1]}")


def text_of(f):
    return " ".join(sstr(f.get(k)) for k in ("title", "issue", "problem"))


def tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9_]{3,}", text.lower()):
        if w in STOP:
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def similarity(a, b):
    ta, tb = tokens(text_of(a)), tokens(text_of(b))
    if not ta or not tb:
        return 0.0
    full_sim = len(ta & tb) / len(ta | tb)
    has_body_a = bool(sstr(a.get("issue")).strip() or sstr(a.get("problem")).strip())
    has_body_b = bool(sstr(b.get("issue")).strip() or sstr(b.get("problem")).strip())
    if not (has_body_a and has_body_b):
        tta, ttb = tokens(sstr(a.get("title"))), tokens(sstr(b.get("title")))
        if tta and ttb:
            return max(full_sim, len(tta & ttb) / len(tta | ttb))
    return full_sim


def near(a, b):
    """True when some site of a and some site of b are in one file within NEAR_LINES. None: no common file."""
    same_file, is_near = False, False
    for pa, ra in sites(a):
        for pb, rb in sites(b):
            if pa != pb:
                continue
            same_file = True
            if ra and rb and max(ra[0], rb[0]) - min(ra[1], rb[1]) <= NEAR_LINES:
                is_near = True
    return is_near if same_file else None


def match_score(n, p):
    """Return the word overlap when n certainly matches p, else None."""
    is_near = near(n, p)
    if is_near is None:
        return None
    sim = similarity(n, p)
    if is_near:
        need = SIM_NEAR_DECLINE if p.get("status") == "declined" else SIM_NEAR
    else:
        need = SIM_FAR
    return sim if sim >= need else None


def letter_for(f):
    return LANE_LETTER.get(sstr(f.get("lane")), OTHER_LETTER)


def id_key(i):
    m = STABLE_ID.match(i)
    return (m.group(1), int(m.group(2))) if m else (i, 0)


def sev_of(f, where):
    sev = sstr(f.get("severity")).upper()
    if sev not in SEV_RANK:
        raise BadInput(f"{where}: invalid or missing severity {f.get('severity')!r}")
    return sev


def _has_location(f):
    if isinstance(f.get("location"), str) and f["location"].strip():
        return True
    if isinstance(f.get("file"), str) and f["file"].strip():
        return True
    return False


def _validate_locations_field(f, where):
    if "locations" in f and f["locations"] is not None:
        locs = f["locations"]
        if not isinstance(locs, list) or not all(isinstance(x, str) for x in locs):
            raise BadInput(f"{where}: locations must be a list of strings")


def _assign_display_ids(todo):
    todo.sort(key=lambda f: (SEV_RANK.get(sstr(f.get("severity")).upper(), 99), id_key(sstr(f.get("id")))))
    counters = {}
    for f in todo:
        letter = SEV_LETTER.get(sstr(f.get("severity")).upper(), "X")
        counters[letter] = counters.get(letter, 0) + 1
        f["display_id"] = f"{letter}{counters[letter]}"


# ---------------------------------------------------------------- previous state

def normalise_previous(doc):
    """Return (state, id_map, warnings). state: {pr, last_sha, previous, findings, next_id}."""
    warnings, id_map = [], {}
    if isinstance(doc, list):
        doc = {"findings": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("findings", []), list):
        raise BadInput("previous findings.json must be an object with a findings list")
    findings = [dict(f) if isinstance(f, dict) else f for f in doc.get("findings", [])]
    next_id = {}
    if "next_id" in doc and doc["next_id"] is not None:
        raw_next = doc["next_id"]
        if not isinstance(raw_next, dict):
            raise BadInput("next_id must be an object of letter to number")
        for k, v in raw_next.items():
            if not (isinstance(k, str) and k in VALID_LETTERS and isinstance(v, int)
                    and not isinstance(v, bool) and v >= 1):
                raise BadInput(f"next_id entry {k!r}: {v!r} is not a valid lane letter and a whole number from 1")
            next_id[k] = v
    if "previous" in doc and doc["previous"] is not None and not isinstance(doc["previous"], list):
        raise BadInput("previous must be a list")
    if "cleared" in doc and doc["cleared"] is not None and not isinstance(doc["cleared"], list):
        raise BadInput("cleared must be a list")

    def bump(i):
        m = STABLE_ID.match(sstr(i))
        if m:
            next_id[m.group(1)] = max(next_id.get(m.group(1), 1), int(m.group(2)) + 1)

    for f in findings:
        if not isinstance(f, dict):
            raise BadInput("every previous finding must be an object")
    # ids seen anywhere count, whatever the status, so an id is never reused
    for f in findings:
        bump(f.get("id", ""))
    for c in doc.get("cleared") or []:
        if isinstance(c, dict):
            bump(c.get("id", ""))
    for p in doc.get("previous") or []:
        bump(p.get("id", "") if isinstance(p, dict) else p)

    seen_stable = set()
    seen_legacy = set()
    for idx, f in enumerate(findings):
        where = f"previous finding #{idx + 1} ({f.get('id', 'no id')})"
        f["severity"] = sev_of(f, where)
        if not _has_location(f):
            raise BadInput(f"{where}: no location")
        _validate_locations_field(f, where)
        raw_st = f.get("status")
        st = "todo" if raw_st is None else raw_st
        if not isinstance(st, str) or st not in STATUSES:
            raise BadInput(f"{where}: invalid status {st!r} (use todo, fixed or declined)")
        f["status"] = st
        for key in ("reason", "challenge"):
            if key in f and f[key] is not None and not isinstance(f[key], str):
                raise BadInput(f"{where}: {key} must be a string")
        if st == "declined" and not sstr(f.get("reason")).strip():
            warnings.append(f"{where}: declined with no reason; recorded as '(no reason recorded)'")
            f["reason"] = "(no reason recorded)"
        old_raw = f.get("id")
        if old_raw is not None and not isinstance(old_raw, str):
            raise BadInput(f"{where}: id must be a string")
        old = sstr(old_raw).strip()
        if STABLE_ID.match(old):
            if old in seen_stable:
                raise BadInput(f"duplicate id {old} in previous findings")
            seen_stable.add(old)
            f["id"] = old
        elif old:
            if old in seen_legacy:
                raise BadInput(f"duplicate id {old} in previous findings")
            seen_legacy.add(old)
            f["id"] = old
    for idx, f in enumerate(findings):
        old = sstr(f.get("id")).strip()
        if not STABLE_ID.match(old):
            letter = letter_for(f)
            new = f"{letter}{next_id.get(letter, 1)}"
            next_id[letter] = next_id.get(letter, 1) + 1
            if old:
                id_map[old] = new
            f["id"] = new
            f["legacy"] = True
            f.pop("display_id", None)
        findings[idx] = redact_finding(f)
    state = {"pr": doc.get("pr"), "last_sha": doc.get("last_sha"),
             "previous": doc.get("previous") or [], "findings": findings, "next_id": next_id}
    return state, id_map, warnings


def empty_state():
    return {"pr": None, "last_sha": None, "previous": [], "findings": [], "next_id": {}}


def load_previous(path):
    if path == "-":
        return empty_state(), {}, []
    return normalise_previous(load_json(path, "previous findings.json"))


# ---------------------------------------------------------------- prepare

def decline_row(f):
    file_, lines = file_and_lines(f)
    return {"id": f["id"], "file": file_, "lines": lines, "severity": f["severity"],
            "issue": redact(f.get("issue") or f.get("title") or f.get("problem") or "", handles=True),
            "reason": redact(f.get("reason", ""), handles=True)}


def lanes_of(f):
    out = []
    raw = [f.get("lane")] + (list(f.get("sources")) if isinstance(f.get("sources"), list) else [])
    for x in raw:
        if not isinstance(x, str):
            continue
        x = x.strip()
        if ":" in x:
            x = x.split(":", 1)[1].strip()
        if x in VALID_LANES and x not in out:
            out.append(x)
    return out


def open_row(f):
    file_, lines = file_and_lines(f)
    return {"id": f["id"], "lanes": lanes_of(f), "file": file_, "lines": lines,
            "severity": f["severity"],
            "issue": redact(f.get("issue") or f.get("title") or f.get("problem") or "", handles=True)}


def counts(findings):
    c = {s: sum(1 for f in findings if f["status"] == s) for s in STATUSES}
    return c


def _extract_level_opt(args):
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--level":
            if i + 1 >= len(args):
                die("--level needs PR or CODEBASE")
            lvl = args[i + 1].upper()
            if lvl not in REVIEW_LEVELS:
                die(f"invalid --level {args[i + 1]!r} (use PR or CODEBASE)")
            i += 2
            continue
        if a.startswith("--level="):
            lvl = a.split("=", 1)[1].upper()
            if lvl not in REVIEW_LEVELS:
                die(f"invalid --level {lvl!r} (use PR or CODEBASE)")
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def cmd_prepare(args):
    args = _extract_level_opt(args)
    if len(args) != 2:
        die("usage: carry_findings.py prepare <prev_findings.json|-> <bundle_dir> [--level PR|CODEBASE]")
    prev_path, bundle = args
    state, id_map, warnings = load_previous(prev_path)
    summary = {"previous": "none" if prev_path == "-" else "found", "last_sha": state["last_sha"],
               "next_id": state["next_id"], "id_map": id_map, "warnings": warnings}
    if prev_path == "-":
        summary.update({"findings": 0, "todo": 0, "fixed": 0, "declined": 0, "declines": 0, "open": 0, "files": []})
        print(json.dumps(summary, indent=2))
        return 0
    if not os.path.isdir(bundle):
        raise BadInput(f"bundle_dir is not a directory: {bundle}")
    declines = [decline_row(f) for f in state["findings"] if f["status"] == "declined"]
    open_ = [open_row(f) for f in state["findings"]
             if f["status"] == "todo" and f["severity"] in BLOCKING]
    paths = {"previous": os.path.join(bundle, "previous.json"),
             "declines": os.path.join(bundle, "declines.json"),
             "open": os.path.join(bundle, "open.json")}
    write_json(paths["previous"], state)
    write_json(paths["declines"], declines)
    write_json(paths["open"], open_)
    summary.update(counts(state["findings"]))
    summary.update({"findings": len(state["findings"]), "declines": len(declines), "open": len(open_),
                    "files": sorted(paths.values())})
    print(json.dumps(summary, indent=2))
    return 0


# ---------------------------------------------------------------- reconcile

def _validate_new_inputs(raw_findings, raw_cleared, raw_still, extra=None):
    if not isinstance(raw_findings, list):
        raise BadInput("new_findings must be a list of findings")
    findings = []
    for idx, f in enumerate(raw_findings):
        where = f"new finding #{idx + 1} ({f.get('id', 'no id') if isinstance(f, dict) else '?'})"
        if not isinstance(f, dict):
            raise BadInput(f"{where}: not an object")
        f = dict(f)
        f["severity"] = sev_of(f, where)
        if not _has_location(f):
            raise BadInput(f"{where}: no location")
        _validate_locations_field(f, where)
        findings.append(f)
    if raw_cleared is None:
        cleared = []
    elif isinstance(raw_cleared, list) and all(isinstance(c, dict) for c in raw_cleared):
        cleared = raw_cleared
    else:
        raise BadInput("cleared must be a list of {id, evidence}")
    if raw_still is None:
        still = []
    elif isinstance(raw_still, list) and all(isinstance(s, str) for s in raw_still):
        still = raw_still
    else:
        raise BadInput("still_open must be a list of ids")
    if extra and "previous" in extra and extra["previous"] is not None and not isinstance(extra["previous"], list):
        raise BadInput("previous must be a list")
    return findings, cleared, still


def read_new(path):
    doc = load_json(path, "new findings.json")
    if isinstance(doc, list):
        doc = {"findings": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise BadInput("new findings.json must be an array or an object with a findings list")
    raw_cleared = doc["cleared"] if "cleared" in doc else []
    raw_still = doc["still_open"] if "still_open" in doc else []
    extra = {k: v for k, v in doc.items() if k not in ("findings", "cleared", "still_open")}
    findings, cleared, still = _validate_new_inputs(doc["findings"], raw_cleared, raw_still, extra=extra)
    return findings, cleared, still, extra


def challenge_text(n):
    text = redact(n.get("evidence") or n.get("issue") or n.get("problem") or "", handles=True).strip()
    trig = redact(n.get("trigger"), handles=True).strip()
    if trig and not trig.lower().startswith("n/a"):
        text = f"{text} Trigger: {trig}".strip()
    return text


def _pick_better_challenger(cur_ev, cur_n, new_ev, new_n):
    ev_parts = [cur_ev] if cur_ev else []
    if new_ev and new_ev not in ev_parts:
        ev_parts.append(new_ev)
    combined_ev = " ".join(ev_parts)
    cur_rank = (SEV_RANK[cur_n["severity"]], 0 if sstr(cur_n.get("verified")) == "confirmed" else 1)
    new_rank = (SEV_RANK[new_n["severity"]], 0 if sstr(new_n.get("verified")) == "confirmed" else 1)
    best_n = new_n if new_rank < cur_rank else cur_n
    return combined_ev, best_n


def _apply_challenger_to_reopened(q, cn, ev):
    q["status"] = "todo"
    q["challenge"] = ev
    if _has_location(cn):
        for key in ("location", "locations", "file", "lines"):
            q.pop(key, None)
            if cn.get(key):
                q[key] = cn[key]
    for k in list(q.keys()):
        if k.startswith("eval_"):
            q.pop(k, None)
    for key in ("title", "issue", "problem", "fix", "code_example", "trigger", "verified",
                "confidence", "dimension", "impact", "breaking", "ac_ref", "in_diff",
                "dedupe_key", "introduced_by_pr", "origin"):
        val = cn.get(key)
        if val is not None and (not isinstance(val, str) or val.strip()):
            q[key] = val
    for k, val in cn.items():
        if k.startswith("eval_") and val is not None:
            q[k] = val
    if isinstance(cn.get("sources"), list):
        srcs = list(q.get("sources") or [])
        for s in cn["sources"]:
            if s not in srcs:
                srcs.append(s)
        if srcs:
            q["sources"] = srcs
    if SEV_RANK[cn["severity"]] < SEV_RANK[q["severity"]]:
        q["severity"] = cn["severity"]


def reconcile(prev, new_findings, cleared, still_open, extra):
    notes, warnings = [], []
    summary = {k: [] for k in ("new", "kept", "reopened", "challenged", "dropped_repeat_decline",
                               "reopened_decline", "carried_open", "carried_other", "carried_fixed",
                               "carried_declined", "declined_still_present", "cleared", "not_carried",
                               "ambiguous")}
    extra = dict(extra or {})
    new_findings, cleared, still_open = _validate_new_inputs(new_findings, cleared, still_open, extra=extra)
    pfind = {f["id"]: f for f in prev["findings"]}
    raw_last_sha = extra.get("last_sha")
    if isinstance(raw_last_sha, str) and raw_last_sha.strip():
        last_sha = raw_last_sha.strip()
    elif prev.get("last_sha"):
        last_sha = prev.get("last_sha")
        warnings.append("no last_sha in the new findings; kept previous last_sha")
    else:
        last_sha = None
    next_id = dict(prev["next_id"])

    news = []
    for n in new_findings:
        if sstr(n.get("verified")) == "refuted":
            notes.append("excluded a refuted finding at " + sstr(n.get("location") or n.get("file")))
            continue
        news.append(n)

    # 1. explicit challenges
    disposed = {}   # new index -> ("challenge", decline id, evidence, finding)
    challenges = {}
    for i, n in enumerate(news):
        cid = n.get("challenges")
        if not cid:
            continue
        p = pfind.get(sstr(cid))
        if p is not None and p["status"] == "declined":
            ev = challenge_text(n)
            pid = p["id"]
            disposed[i] = ("challenge", pid, ev, n)
            if pid not in summary["challenged"]:
                summary["challenged"].append(pid)
            if pid in challenges:
                challenges[pid] = _pick_better_challenger(*challenges[pid], ev, n)
            else:
                challenges[pid] = (ev, n)
        else:
            warnings.append(f"challenges {cid} is not an earlier decline; treated as an ordinary finding")
            n.pop("challenges", None)

    # 2. match explicit open/fixed stable ids first, then remaining by file, distance and words
    assigned, used = {}, set()
    for i, n in enumerate(news):
        if i in disposed:
            continue
        nid = n.get("id")
        if isinstance(nid, str) and nid in pfind and pfind[nid]["status"] in ("todo", "fixed") and nid not in used:
            assigned[i] = nid
            used.add(nid)

    cands = {}
    for i, n in enumerate(news):
        if i in disposed or i in assigned:
            continue
        found = []
        for p in prev["findings"]:
            if p["id"] in used:
                continue
            s = match_score(n, p)
            if s is not None:
                found.append((s, p["id"]))
        found.sort(key=lambda t: (-t[0], id_key(t[1])))
        if len(found) >= 2 and found[0][0] - found[1][0] < AMBIGUOUS:
            summary["ambiguous"].append({"at": sstr(n.get("location") or n.get("file")),
                                         "candidates": [found[0][1], found[1][1]]})
            continue
        cands[i] = found
    pairs = sorted(((s, pid, i) for i, found in cands.items() for s, pid in found),
                   key=lambda t: (-t[0], id_key(t[1]), t[2]))
    for s, pid, i in pairs:
        if i in assigned:
            continue
        if pfind[pid]["status"] == "declined":
            assigned[i] = pid   # many findings can repeat one decline
        elif pid not in used:
            assigned[i] = pid
            used.add(pid)

    out_new = []
    raised = set()          # previous ids that a new finding raised again
    out_by_prev = {}
    repeat_meta = {}
    for i, n in enumerate(news):
        if i in disposed:
            continue
        pid = assigned.get(i)
        if pid is None:
            out_new.append(("new", n))
            continue
        p = pfind[pid]
        if p["status"] == "declined":
            ch_entry = challenges.get(pid)
            reopened_by_challenge = ch_entry is not None and (
                p["severity"] in BLOCKING or ch_entry[1]["severity"] in BLOCKING
            )
            if reopened_by_challenge:
                # Duplicate of a decline already reopened by challenge; escalate severity if confirmed higher.
                if sstr(n.get("verified")) == "confirmed" and SEV_RANK[n["severity"]] < SEV_RANK[ch_entry[1]["severity"]]:
                    challenges[pid] = (ch_entry[0], n)
                continue
            if n["severity"] in BLOCKING and sstr(n.get("verified")) == "confirmed":
                # Safety floor: a verified CRITICAL or HIGH is never hidden. The decline
                # stands, because no challenge answered its reason (the verifier saw the
                # claim, not the reason). The report lists it, chat only.
                if pid not in summary["declined_still_present"]:
                    summary["declined_still_present"].append(pid)
                    notes.append(f"{pid}: verified {n['severity']} repeats a decline with no challenge; "
                                 f"the decline stands and the report lists it")
                meta = repeat_meta.setdefault(pid, {"titles": [], "locations": []})
                ntitle = sstr(n.get("title")).strip()
                if ntitle and ntitle not in meta["titles"]:
                    meta["titles"].append(ntitle)
                for loc_val in [n.get("location")] + (list(n.get("locations")) if isinstance(n.get("locations"), list) else []):
                    if isinstance(loc_val, str) and loc_val.strip() and loc_val.strip() not in meta["locations"]:
                        meta["locations"].append(loc_val.strip())
            else:
                summary["dropped_repeat_decline"].append(pid)
            continue
        raised.add(pid)
        o = {k: v for k, v in n.items() if k not in ("challenges", "status", "commit", "reason")}
        o["id"], o["status"] = pid, "todo"
        if p.get("reopened_from"):
            o["reopened_from"] = p["reopened_from"]
        if p["status"] == "fixed":
            o["reopened_from"] = sstr(p.get("commit") or prev.get("last_sha") or "unknown") or "unknown"
            summary["reopened"].append(pid)
        else:
            summary["kept"].append(pid)
            if p["severity"] in BLOCKING and SEV_RANK[p["severity"]] < SEV_RANK[o["severity"]]:
                notes.append(f"{pid}: severity kept at {p['severity']} (new review said {o['severity']})")
                o["severity"] = p["severity"]
                if p.get("legacy") is True and not sstr(o.get("trigger")).strip():
                    o["legacy"] = True
        out_by_prev[pid] = o

    # 3. cleared entries
    clear_by_id = {}
    for c in cleared:
        cid = sstr(c.get("id"))
        ev_raw = c.get("evidence")
        ev = ev_raw.strip() if isinstance(ev_raw, str) else ""
        p = pfind.get(cid)
        if p is None:
            warnings.append(f"cleared {cid}: not an earlier finding; ignored")
        elif p["status"] != "todo":
            warnings.append(f"cleared {cid}: finding is {p['status']}, not todo; ignored")
        elif cid in raised:
            notes.append(f"cleared {cid} ignored: another reviewer raised it again")
        elif not ev:
            warnings.append(f"cleared {cid}: no evidence; the finding is not cleared")
        elif cid not in clear_by_id or (c.get("resolved") is True and clear_by_id[cid].get("resolved") is not True):
            clear_by_id[cid] = c
    still = set()
    for s in still_open:
        p = pfind.get(s)
        if p is None or p["status"] != "todo":
            warnings.append(f"still_open {s}: not an earlier todo finding; ignored")
        else:
            still.add(s)

    # 4. carry the previous findings that were not raised again
    carried = []
    for p in prev["findings"]:
        pid = p["id"]
        if pid in out_by_prev:
            continue
        q = dict(p)
        if p["status"] == "declined":
            if pid in challenges:
                ev, cn = challenges[pid]
                q["challenge"] = ev
                if p["severity"] in BLOCKING or cn["severity"] in BLOCKING:
                    _apply_challenger_to_reopened(q, cn, ev)
                    if pid not in summary["reopened_decline"]:
                        summary["reopened_decline"].append(pid)
                else:
                    summary["carried_declined"].append(pid)
            else:
                summary["carried_declined"].append(pid)
            if q["status"] == "declined" and pid in repeat_meta:
                q["declined_still_present"] = True
                mt = list(q.get("merged_titles") or [])
                for t in repeat_meta[pid]["titles"]:
                    if t != q.get("title") and t not in mt:
                        mt.append(t)
                if mt:
                    q["merged_titles"] = mt
                rl = list(q.get("repeat_locations") or [])
                for loc_val in repeat_meta[pid]["locations"]:
                    if loc_val not in rl:
                        rl.append(loc_val)
                if rl:
                    q["repeat_locations"] = rl
            carried.append(q)
        elif p["status"] == "fixed":
            summary["carried_fixed"].append(pid)
            carried.append(q)
        else:  # todo, not raised again
            c = clear_by_id.get(pid)
            if c is not None:
                summary["cleared"].append(pid)
                if c.get("resolved") is True:
                    q["status"] = "fixed"
                    q["commit"] = sstr(last_sha or "unknown")
                    carried.append(q)
                continue
            if p["severity"] in BLOCKING:
                summary["carried_open"].append(pid)
                carried.append(q)
            elif pid in still:
                summary["carried_other"].append(pid)
                carried.append(q)
            else:
                summary["not_carried"].append(pid)

    # 5. ids for new findings
    for kind, n in out_new:
        letter = letter_for(n)
        o = {k: v for k, v in n.items() if k not in ("challenges", "status", "commit", "reason")}
        o["id"] = f"{letter}{next_id.get(letter, 1)}"
        next_id[letter] = next_id.get(letter, 1) + 1
        o["status"] = "todo"
        out_by_prev["new:" + o["id"]] = o
        summary["new"].append(o["id"])
    # the previous counters already cover every old id; keep the higher value per letter
    for k, v in prev["next_id"].items():
        next_id[k] = max(next_id.get(k, 1), v)

    # 6. display labels (C1, H1, ...) for the todo findings, as the report shows them
    todo = [redact_finding(f) for f in list(out_by_prev.values()) + [q for q in carried if q["status"] == "todo"]]
    _assign_display_ids(todo)
    rest = [redact_finding(q) for q in carried if q["status"] != "todo"]
    for q in rest:
        q.pop("display_id", None)
    rest.sort(key=lambda f: (0 if f["status"] == "declined" else 1, id_key(f["id"])))

    out_cleared = []
    for pid in summary["cleared"]:
        c = clear_by_id[pid]
        row = {"id": pid, "evidence": redact(c["evidence"].strip(), handles=True)}
        if isinstance(c.get("lane"), str) and c["lane"].strip():
            row["lane"] = c["lane"].strip()
        out_cleared.append(row)

    out = {}
    out["pr"] = extra.get("pr") or prev.get("pr")
    if last_sha:
        out["last_sha"] = last_sha
    else:
        warnings.append("no last_sha in the new findings; set last_sha before you write the durable copy")
    for k, v in extra.items():
        if k not in ("pr", "last_sha", "previous", "next_id", "findings", "cleared", "reconcile"):
            out[k] = v
    out["previous"] = extra.get("previous") or []
    out["next_id"] = dict(sorted(next_id.items()))
    out["findings"] = todo + rest
    out["cleared"] = out_cleared
    notes = [redact(m, handles=True) for m in notes]
    warnings = [redact(w, handles=True) for w in warnings]
    summary_out = {"next_id": out["next_id"], "reportable_ids": [f["id"] for f in todo],
                   "counts": {k: len(v) for k, v in summary.items()}, **summary,
                   "notes": notes, "warnings": warnings}
    out["reconcile"] = {k: v for k, v in summary_out.items() if k != "reportable_ids"}
    return out, summary_out, todo


def cmd_reconcile(args):
    args = _extract_level_opt(args)
    reported = None
    if "--reported" in args:
        i = args.index("--reported")
        if i + 1 >= len(args):
            die("--reported needs a file path")
        reported = args[i + 1]
        args = args[:i] + args[i + 2:]
    if len(args) != 3:
        die("usage: carry_findings.py reconcile <prev_findings.json|-> <new_findings.json> <out.json> [--reported <file>] [--level PR|CODEBASE]")
    prev_path, new_path, out_path = args
    prev, _id_map, prev_warnings = load_previous(prev_path)
    findings, cleared, still_open, extra = read_new(new_path)
    out, summary, todo = reconcile(prev, findings, cleared, still_open, extra)
    summary["warnings"] = [redact(w, handles=True) for w in prev_warnings] + summary["warnings"]
    out["reconcile"]["warnings"] = summary["warnings"]
    write_json(out_path, out)
    if reported:
        write_json(reported, todo)
    print(json.dumps(summary, indent=2))
    return 0


# ---------------------------------------------------------------- mark

def cmd_mark(args):
    args = _extract_level_opt(args)
    if len(args) != 2:
        die("usage: carry_findings.py mark <findings.json> <decisions.json> [--level PR|CODEBASE]")
    doc = load_json(args[0], "findings.json")
    decisions = load_json(args[1], "decisions.json")
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise BadInput("findings.json must be an object with a findings list")
    if not isinstance(decisions, list):
        raise BadInput("decisions.json must be a list")
    by_id = {}
    for f in doc["findings"]:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if isinstance(fid, str) and fid:
            if fid in by_id:
                raise BadInput(f"duplicate id {fid} in findings.json")
            by_id[fid] = f
    seen_decisions = set()
    for d in decisions:
        if not isinstance(d, dict) or d.get("status") not in STATUSES:
            raise BadInput(f"bad decision {d!r}: status must be todo, fixed or declined")
        did = d.get("id")
        if not isinstance(did, str) or not did.strip():
            raise BadInput(f"bad decision {d!r}: missing id")
        if did in seen_decisions:
            raise BadInput(f"duplicate decision for id {did!r}")
        seen_decisions.add(did)
        f = by_id.get(did)
        if f is None:
            raise BadInput(f"decision for unknown id {did!r}")
        if d["status"] == "declined" and not (isinstance(d.get("reason"), str) and d["reason"].strip()):
            raise BadInput(f"{did}: a decline needs a reason")
        if d["status"] == "fixed" and not (isinstance(d.get("commit"), str) and d["commit"].strip()):
            raise BadInput(f"{did}: a fix needs a commit sha")
    for d in decisions:
        f = by_id[d["id"]]
        f["status"] = d["status"]
        if d["status"] == "declined":
            f["reason"] = redact(d["reason"].strip(), handles=True)
            f.pop("commit", None)
        elif d["status"] == "fixed":
            f["commit"] = d["commit"].strip()
            for key in ("reason", "challenge", "declined_still_present", "repeat_locations", "merged_titles"):
                f.pop(key, None)
        else:
            for key in ("commit", "reason", "challenge", "declined_still_present", "repeat_locations", "merged_titles"):
                f.pop(key, None)
        if d["status"] != "todo":
            f.pop("display_id", None)
    todo = [f for f in doc["findings"] if isinstance(f, dict) and (f.get("status") or "todo") == "todo"]
    if todo:
        _assign_display_ids(todo)
    write_json(args[0], doc)
    print(json.dumps({"marked": [d["id"] for d in decisions]}))
    return 0


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help"):
        print((__doc__ or "").strip())
        sys.exit(0)
    if len(sys.argv) < 2 or sys.argv[1] not in ("prepare", "reconcile", "mark"):
        die("usage: carry_findings.py prepare|reconcile|mark ... (see --help)")
    if len(sys.argv) >= 3 and any(a in ("-h", "--help") for a in sys.argv[2:]):
        print((__doc__ or "").strip())
        sys.exit(0)
    try:
        sys.exit({"prepare": cmd_prepare, "reconcile": cmd_reconcile, "mark": cmd_mark}[sys.argv[1]](sys.argv[2:]))
    except BadInput as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
