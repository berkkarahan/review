#!/usr/bin/env python3
"""Carry finding state between two reviews of the same PR (stable ids, declines, open findings).

Usage:
  carry_findings.py prepare   <prev_findings.json|-> <bundle_dir>
  carry_findings.py reconcile <prev_findings.json|-> <new_findings.json> <out.json> [--reported <file>]
  carry_findings.py mark      <findings.json> <decisions.json>

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
Python standard library only. The script writes no names. It redacts emails (and @handles in
reason and challenge text) in the strings that it writes.
"""
import json
import os
import re
import sys

SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "QUESTION": 4}
SEV_LETTER = {"CRITICAL": "C", "HIGH": "H", "MEDIUM": "M", "LOW": "L", "QUESTION": "Q"}
BLOCKING = ("CRITICAL", "HIGH")
STATUSES = ("todo", "fixed", "declined")

# Lane letters. None is C, H, M, L or Q, so a stable id never reads as a report label.
LANE_LETTER = {
    "adherence": "B", "business-logic": "B", "code-quality": "D", "security": "S", "performance": "P",
    "compatibility": "A", "robustness": "R", "conventions": "V", "skim": "K",
}
OTHER_LETTER = "X"  # critical-risk, unknown or missing lane
STABLE_ID = re.compile(r"^([BDSPARVKX])(\d+)$")

NEAR_LINES = 5          # same flaw sits within 5 lines (the G5 distance)
SIM_NEAR = 0.30         # word overlap needed when the sites are near
SIM_NEAR_DECLINE = 0.45  # stricter when the match would drop a finding
SIM_FAR = 0.60          # word overlap needed when the sites are not near
AMBIGUOUS = 0.05        # two candidates this close: not certain, so the finding is new

LOC = re.compile(r"^(?P<path>[^\s:]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HANDLE = re.compile(r"(?<![\w.@/])@[A-Za-z0-9][A-Za-z0-9-]{0,38}(?!\w|\.\w|[(/-])")
STOP = set("the and for with that this from are not but has have was were when then than into "
           "its can will may all any one two new old use used uses get gets set sets via per".split())


class BadInput(Exception):
    pass


def die(msg):
    sys.stderr.write("carry_findings: error: " + msg + "\n")
    sys.exit(2)


# ---------------------------------------------------------------- helpers

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
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def redact(text, handles=False):
    text = EMAIL.sub("<email>", str(text))
    if handles:
        text = HANDLE.sub("<handle>", text)
    return text


def norm_path(p):
    p = str(p).strip()
    while p.startswith("head/"):
        p = p[5:]
    return p


def parse_lines(text):
    m = re.match(r"^(\d+)(?:-(\d+))?$", str(text).strip())
    if not m:
        return None
    a = int(m.group(1))
    return (a, int(m.group(2) or a))


def sites(f):
    """List of (path, (a, b) | None) for location, locations and file/lines."""
    out = []
    if f.get("file"):
        out.append((norm_path(f["file"]), parse_lines(f.get("lines", ""))))
    for loc in [f.get("location")] + list(f.get("locations") or []):
        if not loc:
            continue
        loc = str(loc).strip()
        m = LOC.match(loc)
        if m:
            a = int(m.group("a"))
            out.append((norm_path(m.group("path")), (a, int(m.group("b") or a))))
            continue
        first = loc.split()[0] if loc.split() else ""
        if "/" in first or "." in first:
            out.append((norm_path(first), None))
        else:
            out.append((loc, None))
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
    return " ".join(str(f.get(k, "")) for k in ("title", "issue", "problem"))


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
    return len(ta & tb) / len(ta | tb)


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
    return LANE_LETTER.get(str(f.get("lane", "")), OTHER_LETTER)


def id_key(i):
    m = STABLE_ID.match(i)
    return (m.group(1), int(m.group(2))) if m else (i, 0)


def sev_of(f, where):
    sev = str(f.get("severity", "")).upper()
    if sev not in SEV_RANK:
        raise BadInput(f"{where}: invalid or missing severity {f.get('severity')!r}")
    return sev


# ---------------------------------------------------------------- previous state

def normalise_previous(doc):
    """Return (state, id_map, warnings). state: {pr, last_sha, previous, findings, next_id}."""
    warnings, id_map = [], {}
    if isinstance(doc, list):
        doc = {"findings": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("findings", []), list):
        raise BadInput("previous findings.json must be an object with a findings list")
    findings = [dict(f) for f in doc.get("findings", [])]
    next_id = {}
    raw_next = doc.get("next_id") or {}
    if not isinstance(raw_next, dict):
        raise BadInput("next_id must be an object of letter to number")
    for k, v in raw_next.items():
        if not (isinstance(k, str) and len(k) == 1 and isinstance(v, int) and not isinstance(v, bool) and v >= 1):
            raise BadInput(f"next_id entry {k!r}: {v!r} is not a letter and a whole number from 1")
        next_id[k] = v

    def bump(i):
        m = STABLE_ID.match(str(i))
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

    seen = set()
    for idx, f in enumerate(findings):
        where = f"previous finding #{idx + 1} ({f.get('id', 'no id')})"
        f["severity"] = sev_of(f, where)
        if not (f.get("location") or f.get("file")):
            raise BadInput(f"{where}: no location")
        st = f.get("status", "todo")
        if st not in STATUSES:
            raise BadInput(f"{where}: invalid status {st!r} (use todo, fixed or declined)")
        f["status"] = st
        for key in ("reason", "challenge"):
            if f.get(key):
                f[key] = redact(f[key], handles=True)
        if st == "declined" and not str(f.get("reason", "")).strip():
            warnings.append(f"{where}: declined with no reason; recorded as '(no reason recorded)'")
            f["reason"] = "(no reason recorded)"
        old = str(f.get("id", ""))
        if STABLE_ID.match(old):
            if old in seen:
                raise BadInput(f"duplicate id {old} in previous findings")
            seen.add(old)
    for f in findings:
        old = str(f.get("id", ""))
        if STABLE_ID.match(old):
            continue
        letter = letter_for(f)
        new = f"{letter}{next_id.get(letter, 1)}"
        next_id[letter] = next_id.get(letter, 1) + 1
        if old:
            id_map[old] = new
        f["id"] = new
        f.pop("display_id", None)
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
            "issue": redact(f.get("issue") or f.get("title") or f.get("problem") or ""),
            "reason": redact(f.get("reason", ""), handles=True)}


def lanes_of(f):
    out = []
    for x in [f.get("lane")] + list(f.get("sources") or []):
        if x in LANE_LETTER and x not in out:
            out.append(x)
    return out


def open_row(f):
    file_, lines = file_and_lines(f)
    return {"id": f["id"], "lanes": lanes_of(f), "file": file_, "lines": lines,
            "severity": f["severity"],
            "issue": redact(f.get("issue") or f.get("title") or f.get("problem") or "")}


def counts(findings):
    c = {s: sum(1 for f in findings if f["status"] == s) for s in STATUSES}
    return c


def cmd_prepare(args):
    if len(args) != 2:
        die("usage: carry_findings.py prepare <prev_findings.json|-> <bundle_dir>")
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

def read_new(path):
    doc = load_json(path, "new findings.json")
    if isinstance(doc, list):
        doc = {"findings": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise BadInput("new findings.json must be an array or an object with a findings list")
    findings = []
    for idx, f in enumerate(doc["findings"]):
        where = f"new finding #{idx + 1} ({f.get('id', 'no id') if isinstance(f, dict) else '?'})"
        if not isinstance(f, dict):
            raise BadInput(f"{where}: not an object")
        f = dict(f)
        f["severity"] = sev_of(f, where)
        if not (f.get("location") or f.get("file")):
            raise BadInput(f"{where}: no location")
        findings.append(f)
    cleared = doc.get("cleared") or []
    still = doc.get("still_open") or []
    if not isinstance(cleared, list) or not all(isinstance(c, dict) for c in cleared):
        raise BadInput("cleared must be a list of {id, evidence}")
    if not isinstance(still, list) or not all(isinstance(s, str) for s in still):
        raise BadInput("still_open must be a list of ids")
    extra = {k: v for k, v in doc.items() if k not in ("findings", "cleared", "still_open")}
    return findings, cleared, still, extra


def challenge_text(n):
    text = redact(n.get("evidence") or n.get("issue") or n.get("problem") or "", handles=True)
    trig = redact(n.get("trigger", ""), handles=True).strip()
    if trig and not trig.lower().startswith("n/a"):
        text = f"{text} Trigger: {trig}".strip()
    return text


def reconcile(prev, new_findings, cleared, still_open, extra):
    notes, warnings = [], []
    summary = {k: [] for k in ("new", "kept", "reopened", "challenged", "dropped_repeat_decline",
                               "reopened_decline", "carried_open", "carried_other", "carried_fixed",
                               "carried_declined", "declined_still_present", "cleared", "not_carried",
                               "ambiguous")}
    pfind = {f["id"]: f for f in prev["findings"]}
    last_sha = extra.get("last_sha") or prev.get("last_sha")
    next_id = dict(prev["next_id"])

    news = []
    for n in new_findings:
        if str(n.get("verified", "")) == "refuted":
            notes.append("excluded a refuted finding at " + str(n.get("location", n.get("file"))))
            continue
        news.append(n)

    # 1. explicit challenges
    disposed = {}   # new index -> ("challenge", decline id, evidence) | ("repeat", decline id)
    for i, n in enumerate(news):
        cid = n.get("challenges")
        if not cid:
            continue
        p = pfind.get(str(cid))
        if p is not None and p["status"] == "declined":
            disposed[i] = ("challenge", p["id"], challenge_text(n), n)
        else:
            warnings.append(f"challenges {cid} is not an earlier decline; treated as an ordinary finding")
            n.pop("challenges", None)

    # 2. match the rest by file, distance and words
    cands = {}
    for i, n in enumerate(news):
        if i in disposed:
            continue
        found = []
        for p in prev["findings"]:
            s = match_score(n, p)
            if s is not None:
                found.append((s, p["id"]))
        found.sort(key=lambda t: (-t[0], id_key(t[1])))
        if len(found) >= 2 and found[0][0] - found[1][0] < AMBIGUOUS:
            summary["ambiguous"].append({"at": str(n.get("location", n.get("file"))),
                                         "candidates": [found[0][1], found[1][1]]})
            continue
        cands[i] = found
    pairs = sorted(((s, pid, i) for i, found in cands.items() for s, pid in found),
                   key=lambda t: (-t[0], id_key(t[1]), t[2]))
    assigned, used = {}, set()
    for s, pid, i in pairs:
        if i in assigned:
            continue
        if pfind[pid]["status"] == "declined":
            assigned[i] = pid   # many findings can repeat one decline
        elif pid not in used:
            assigned[i] = pid
            used.add(pid)

    out_new, challenges = [], {}
    raised = set()          # previous ids that a new finding raised again
    out_by_prev = {}
    for i, n in enumerate(news):
        if i in disposed:
            _, pid, ev, _n = disposed[i]
            challenges[pid] = (ev, n)
            summary["challenged"].append(pid)
            continue
        pid = assigned.get(i)
        if pid is None:
            out_new.append(("new", n))
            continue
        p = pfind[pid]
        if p["status"] == "declined":
            if n["severity"] in BLOCKING and str(n.get("verified", "")) == "confirmed":
                # Safety floor: a verified CRITICAL or HIGH is never hidden. The decline
                # stands, because no challenge answered its reason (the verifier saw the
                # claim, not the reason). The report lists it, chat only.
                summary["declined_still_present"].append(pid)
                notes.append(f"{pid}: verified {n['severity']} repeats a decline with no challenge; "
                             f"the decline stands and the report lists it")
            else:
                summary["dropped_repeat_decline"].append(pid)
            continue
        raised.add(pid)
        o = {k: v for k, v in n.items() if k not in ("challenges", "status", "commit", "reason")}
        o["id"], o["status"] = pid, "todo"
        if p.get("reopened_from"):
            o["reopened_from"] = p["reopened_from"]
        if p["status"] == "fixed":
            o["reopened_from"] = str(p.get("commit") or prev.get("last_sha") or "unknown")
            summary["reopened"].append(pid)
        else:
            summary["kept"].append(pid)
            if p["severity"] in BLOCKING and SEV_RANK[p["severity"]] < SEV_RANK[o["severity"]]:
                notes.append(f"{pid}: severity kept at {p['severity']} (new review said {o['severity']})")
                o["severity"] = p["severity"]
        out_by_prev[pid] = o

    # 3. cleared entries
    clear_by_id = {}
    for c in cleared:
        cid = str(c.get("id", ""))
        ev = str(c.get("evidence", "")).strip()
        p = pfind.get(cid)
        if p is None:
            warnings.append(f"cleared {cid}: not an earlier finding; ignored")
        elif p["status"] != "todo":
            warnings.append(f"cleared {cid}: finding is {p['status']}, not todo; ignored")
        elif cid in raised:
            notes.append(f"cleared {cid} ignored: another reviewer raised it again")
        elif not ev:
            warnings.append(f"cleared {cid}: no evidence; the finding is not cleared")
        elif cid not in clear_by_id:
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
                if p["severity"] in BLOCKING:
                    q["status"] = "todo"
                    # the earlier anchor is stale: take the place and the proof from the challenger
                    if cn.get("location") or cn.get("file"):
                        for key in ("location", "locations", "file", "lines"):
                            q.pop(key, None)
                            if cn.get(key):
                                q[key] = cn[key]
                    for key in ("trigger", "verified"):
                        if cn.get(key):
                            q[key] = cn[key]
                    if SEV_RANK[cn["severity"]] < SEV_RANK[q["severity"]]:
                        q["severity"] = cn["severity"]
                    if pid not in summary["reopened_decline"]:
                        summary["reopened_decline"].append(pid)
                else:
                    summary["carried_declined"].append(pid)
            else:
                summary["carried_declined"].append(pid)
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
                    q["commit"] = str(last_sha or "unknown")
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
    # decline challenged but also matched by a repeat that was dropped: nothing more to do

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
    todo = list(out_by_prev.values()) + [q for q in carried if q["status"] == "todo"]
    todo.sort(key=lambda f: (SEV_RANK[f["severity"]], id_key(f["id"])))
    counters = {}
    for f in todo:
        letter = SEV_LETTER[f["severity"]]
        counters[letter] = counters.get(letter, 0) + 1
        f["display_id"] = f"{letter}{counters[letter]}"
    rest = [q for q in carried if q["status"] != "todo"]
    for q in rest:
        q.pop("display_id", None)
    rest.sort(key=lambda f: (0 if f["status"] == "declined" else 1, id_key(f["id"])))

    out_cleared = []
    for pid in summary["cleared"]:
        c = clear_by_id[pid]
        row = {"id": pid, "evidence": redact(str(c["evidence"]).strip(), handles=True)}
        if c.get("lane"):
            row["lane"] = str(c["lane"])
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
    out["previous"] = extra.get("previous", [])
    out["next_id"] = dict(sorted(next_id.items()))
    out["findings"] = todo + rest
    out["cleared"] = out_cleared
    summary_out = {"next_id": out["next_id"], "reportable_ids": [f["id"] for f in todo],
                   "counts": {k: len(v) for k, v in summary.items()}, **summary,
                   "notes": notes, "warnings": warnings}
    out["reconcile"] = {k: v for k, v in summary_out.items() if k != "reportable_ids"}
    return out, summary_out, todo


def cmd_reconcile(args):
    reported = None
    if "--reported" in args:
        i = args.index("--reported")
        if i + 1 >= len(args):
            die("--reported needs a file path")
        reported = args[i + 1]
        args = args[:i] + args[i + 2:]
    if len(args) != 3:
        die("usage: carry_findings.py reconcile <prev_findings.json|-> <new_findings.json> <out.json> [--reported <file>]")
    prev_path, new_path, out_path = args
    prev, _id_map, prev_warnings = load_previous(prev_path)
    findings, cleared, still_open, extra = read_new(new_path)
    out, summary, todo = reconcile(prev, findings, cleared, still_open, extra)
    summary["warnings"] = prev_warnings + summary["warnings"]
    out["reconcile"]["warnings"] = summary["warnings"]
    write_json(out_path, out)
    if reported:
        write_json(reported, todo)
    print(json.dumps(summary, indent=2))
    return 0


# ---------------------------------------------------------------- mark

def cmd_mark(args):
    if len(args) != 2:
        die("usage: carry_findings.py mark <findings.json> <decisions.json>")
    doc = load_json(args[0], "findings.json")
    decisions = load_json(args[1], "decisions.json")
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise BadInput("findings.json must be an object with a findings list")
    if not isinstance(decisions, list):
        raise BadInput("decisions.json must be a list")
    by_id = {f.get("id"): f for f in doc["findings"] if isinstance(f, dict)}
    for d in decisions:
        if not isinstance(d, dict) or d.get("status") not in STATUSES:
            raise BadInput(f"bad decision {d!r}: status must be todo, fixed or declined")
        f = by_id.get(d.get("id"))
        if f is None:
            raise BadInput(f"decision for unknown id {d.get('id')!r}")
        if d["status"] == "declined" and not str(d.get("reason", "")).strip():
            raise BadInput(f"{d['id']}: a decline needs a reason")
        if d["status"] == "fixed" and not str(d.get("commit", "")).strip():
            raise BadInput(f"{d['id']}: a fix needs a commit sha")
    for d in decisions:
        f = by_id[d["id"]]
        f["status"] = d["status"]
        if d["status"] == "declined":
            f["reason"] = redact(str(d["reason"]).strip(), handles=True)
            f.pop("commit", None)
        elif d["status"] == "fixed":
            f["commit"] = str(d["commit"]).strip()
        else:
            f.pop("commit", None)
        if d["status"] != "todo":
            f.pop("display_id", None)
    write_json(args[0], doc)
    print(json.dumps({"marked": [d["id"] for d in decisions]}))
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("prepare", "reconcile", "mark"):
        die("usage: carry_findings.py prepare|reconcile|mark ... (see the file header)")
    try:
        sys.exit({"prepare": cmd_prepare, "reconcile": cmd_reconcile, "mark": cmd_mark}[sys.argv[1]](sys.argv[2:]))
    except BadInput as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
