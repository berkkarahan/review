#!/usr/bin/env python3
"""Deterministic STE checks (gate G8). It does not replace the ASD-STE100 standard.

Usage: ste_lint.py <report-or-body.md> [--findings findings.json] [--level PR|CODEBASE] [--json]
Checks, outside verbatim items (code spans, fences, URLs, paths, IDs, quotes):
  - sentence length: 25 words max, 20 words max for an instruction (starts with a verb in IMPERATIVE_START)
  - "-ing" words that are not in the allowlist (including headings)
  - semicolons, chat abbreviations, hedges
  - paragraph length: 6 sentences max
  - question refreshers: Questions for the author and ID-citing questions include 1 refresher sentence
  - evidence: with --findings, every location (path:line) of a kept finding is still in the text
Exit code: 0 clean, 1 failures (printed one per line as "line N: <rule>: <text>"), 2 usage/I/O error.
Stdlib only. Heuristics can have false positives. A human or the lead decides the edge cases.
"""
import argparse
import json
import re
import sys

ING_ALLOW = {
    "pending", "string", "strings", "during", "something", "nothing", "anything", "everything",
    "opening", "openings", "remaining", "building", "buildings", "thing", "things",
    "king", "kings", "ring", "rings", "spring", "springs", "bring",
    "morning", "evening", "ceiling", "ceilings", "wiring", "warning", "warnings",
    "heading", "headings", "meaning", "finding", "passing", "failing",
    "logging", "routing", "mapping", "mappings", "caching", "indexing", "pagination", "padding",
    "binding", "bindings", "posting", "casting", "tracking", "encoding", "encodings",
    "decoding", "decodings", "timing", "timings", "setting", "settings",
    "ping", "lighting", "hashing", "scheduling", "sibling", "siblings",
}
HEDGES = [
    r"\bi think\b", r"\bmaybe\b", r"\bhow about\b", r"\bi am not sure\b", r"\bi['’]m not sure\b",
    r"\bperhaps\b", r"\bprobably\b", r"\bit seems\b", r"\bwdyt\b", r"\bimo\b", r"\bfyi\b", r"\bimho\b",
]
IMPERATIVE_START = {
    "add", "remove", "change", "write", "close", "use", "make", "move", "set", "replace", "delete",
    "keep", "wrap", "call", "check", "run", "rename", "extract", "split", "merge", "update",
    "fix", "do", "put", "return", "raise", "pass", "create", "define", "document", "test",
    "assert", "validate", "limit", "cap", "avoid", "stop", "start", "read", "load", "send",
    "reject", "enforce", "ensure", "prevent", "handle", "guard", "verify", "sanitize", "escape",
    "require", "specify", "include", "exclude", "omit", "restore", "preserve", "pin", "restrict",
    "protect", "clarify", "explain", "ignore", "never", "always",
}
FENCE = re.compile(r"```.*?```", re.S)
CODE = re.compile(r"``(?:[^`\n]|`(?!`))+?``|`[^`\n]*`")
URL = re.compile(r"https?://\S+")
MD_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
ID = re.compile(
    r"\b(?:FR|NFR|AC|OQ|AD|PD|[BDSPARVKXCHMLQTY])-?\d+[\w.-]*\b"
    r"|\b[A-Z][A-Z0-9]{1,9}(?:-[A-Z0-9]{1,10})*-\d+[A-Za-z0-9]*\b"
    r"|\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b"
)
QUOTE = re.compile(r'"[^"\n]*"|“[^”\n]*”')
HUNK_PATHLIKE = re.compile(r"(?:[^\s(\[<*'\"]+/)*[^\s(\[<*'\"]+\s+@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@")
PATHLIKE = re.compile(
    r"(?:[^\s(\[<*'\"]+/)*[^\s(\[<*'\"]+\.\w+(?::\d+(?:-\d+)?)?(?=\s|$|[.,;:!?)\]\">'])"
    r"|[^\s(\[<*'\"]+:\d+(?:-\d+)?(?=\s|$|[.,;:!?)\]\">'])"
)
PREFIX = re.compile(
    r"^\s*(?:[-*]\s+|\d+\.\s+|>\s*)*"
    r"(?:\[[A-Z]+(?:\s*/\s*[A-Z]+)?\]\s*|(?:NIT|Nit):\s*|\*\*(?:Fix|Ask):\*\*\s*|(?:Fix|Ask):\s*)?"
)
SENT_PREFIX = re.compile(r"^(?:\*\*(?:Fix|Ask):\*\*\s*|(?:Fix|Ask):\s*)")
QUESTIONS_HEADING = re.compile(r"^#{1,6}\s+(?:Questions for the (?:author|maintainers)|Open questions)\b", re.I)
LIST_ITEM = re.compile(r"^(\s*(?:>\s*)*)(?:\d+\.|[-*])\s+")
NONLIST_Q_START = re.compile(r"^\s*(?:>\s*)*(?:\*\*Q\d+:?\*\*|Q\d+:?)(?:\s*\([^)]+\))?\s*(?:—|--|:|-)\s*")
Q_LABEL_PREFIX = re.compile(
    r"^\s*(?:>\s*)*(?:(?:\d+\.|[-*])\s+)?"
    r"(?:(?:\*\*Q\d+:?\*\*|Q\d+:?)(?:\s*\([^)]+\))?\s*(?:—|--|:|-)\s*)?"
)
ITEM_ID_RX = re.compile(r"\b(?:AC|FR|NFR|OQ|AD|PD|[BDSPARVKXCHMLQTY])-?\d+\b")
PURE_ID_LIST_RX = re.compile(
    r"^\s*(?:AC|FR|NFR|OQ|AD|PD|[BDSPARVKXCHMLQTY])-?\d+"
    r"(?:\s*(?:,|;|/|and|or)\s*(?:AC|FR|NFR|OQ|AD|PD|[BDSPARVKXCHMLQTY])-?\d+)*\s*$"
)
S3_STORAGE_RX = re.compile(
    r"\b(?:in|to|from|on|via|with|use|using|into)\s+S3\b|\bAWS\s+S3\b"
    r"|\bS3\s+(?:bucket|object|key|store|storage|client|upload|url|uri|path|prefix)\b"
)
V_API_RX = re.compile(
    r"\b(?:API|version|use|using|migrate|move|upgrade|from|to|on)\s+V-?\d+\b"
    r"|\bV-?\d+\s+(?:endpoint|route|api|schema|contract|version|format|payload)\b"
)
META_LINE_RX = re.compile(
    r"^\s*(?:>\s*)*\*\*(?:Top items|Counts|Context|Gaps|Scope|Change|Warning|High risk|"
    r"Override note|Review safety|Earlier declines still present|Earlier findings[^*]*|"
    r"Out of scope or not expected|PR claims[^*]*):\*\*"
)
MIDDLE_DOT_META_RX = re.compile(
    r"^\s*(?:>\s*)*\*\*(?:Top items|Counts|Context|Gaps|Scope|Earlier declines still present):\*\*"
)
FINDING_HEADER_RX = re.compile(
    r"^\s*(?:>\s*)*(?:[🔴🟡🟢⚪]\s*)?\*\*(?:[CHMLQ]|[BDSPARVKX])\d+\s*(?:·|—|:|-)\s*"
)
LIST_INTRO_RX = re.compile(r"^\s*(?:>\s*)*(?:Not posted \(chat only\)|Additional notes \(chat only\)):\s*$")
FENCE_LINE_RX = re.compile(r"^\s*(?:>\s*)*(```+|~~~+)(.*)$")
WORD_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
H2_VERDICT_RX = re.compile(r"^#{2}\s+[🔴🟡🟢]\s+(.+?)\s+—\s+.+$")
HEADING_RX = re.compile(r"^#{1,6}\s+(.*)$")


def _sub_url(repl_char):
    def _fn(m):
        raw = m.group(0)
        core = raw.rstrip(".,;:!?>\"'")
        if core.endswith(")") and "(" not in core:
            core = core[:-1]
        trail = raw[len(core):]
        return f" {repl_char} {trail}"
    return _fn


def _sub_quote(repl_char):
    def _fn(m):
        raw = m.group(0)
        inner = raw[1:-1]
        if inner.endswith((".", "!", "?")):
            return f" {repl_char}{inner[-1]}"
        return f" {repl_char} "
    return _fn


def clean(text):
    text = FENCE.sub(" X ", text)
    text = CODE.sub(" X ", text)
    text = URL.sub(_sub_url("X"), text)
    text = MD_LINK.sub(r" \1 ", text)
    text = QUOTE.sub(_sub_quote("X"), text)
    text = HUNK_PATHLIKE.sub(" X ", text)
    text = PATHLIKE.sub(" X ", text)
    text = ID.sub(" X ", text)
    return text


def clean_keep_ids(text):
    text = FENCE.sub(" ", text)
    text = URL.sub(_sub_url(""), text)
    text = MD_LINK.sub(r" \1 ", text)

    def _unwrap_code(m):
        raw = m.group(0)
        ticks = 2 if raw.startswith("``") else 1
        inner = raw[ticks:-ticks]
        if PURE_ID_LIST_RX.match(inner):
            return f" {inner.strip()} "
        return " "

    def _unwrap_quote(m):
        raw = m.group(0)
        inner = raw[1:-1]
        if PURE_ID_LIST_RX.match(inner):
            return f" {inner.strip()} "
        if inner.endswith((".", "!", "?")):
            return inner[-1]
        return " "

    text = CODE.sub(_unwrap_code, text)
    text = QUOTE.sub(_unwrap_quote, text)
    text = HUNK_PATHLIKE.sub(" ", text)
    text = PATHLIKE.sub(" ", text)
    return text


def extract_question_ids(text):
    """Return item IDs cited in text, filtering common non-ID tokens like AWS S3 or API V2."""
    out = []
    for m in ITEM_ID_RX.finditer(text):
        tok = m.group(0)
        ctx = text[max(0, m.start() - 20):min(len(text), m.end() + 25)]
        if tok == "S3" and S3_STORAGE_RX.search(ctx):
            continue
        if tok.startswith("V") and tok[1:].lstrip("-").isdigit() and V_API_RX.search(ctx):
            continue
        if tok not in out:
            out.append(tok)
    return out


def _word_count_non_id(fragment):
    return len(re.findall(r"[A-Za-z][A-Za-z'’-]*", clean(fragment)))


def has_inline_refresher(raw_no_label, required_id=None):
    """True if raw_no_label has a >=4-word inline refresher before ':'/'—'/'--'/' - ' or in '(...)' before '?'."""
    q_mark = raw_no_label.find("?")
    if q_mark <= 0:
        return False
    prefix = raw_no_label[:q_mark].strip()
    for sep in ("—", "--", ":", " - "):
        if sep in prefix:
            before_sep = prefix.rsplit(sep, 1)[0]
            if _word_count_non_id(before_sep) >= 4:
                if required_id is None or re.search(rf"\b{re.escape(required_id)}\b", before_sep):
                    return True
    for pm in re.finditer(r"\(([^)]+)\)", prefix):
        paren = pm.group(1)
        if _word_count_non_id(paren) >= 4:
            if required_id is None:
                return True
            before_or_in = prefix[:pm.end()]
            if re.search(rf"\b{re.escape(required_id)}\b", before_or_in):
                return True
    return False


def sentences(par):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\[`\"'(*“‘_⚠️])", par) if s.strip()]


def _check_sentence(s, line_no, issues, is_heading=False):
    s_chk = SENT_PREFIX.sub("", s).strip()
    if not s_chk:
        return
    words = WORD_RX.findall(s_chk)
    first = words[0].lower() if words else ""
    limit = 20 if (not is_heading and first in IMPERATIVE_START) else 25
    if len(words) > limit:
        issues.append((line_no, f"sentence-over-{limit}-words", " ".join(words[:12]) + " ..."))
    if ";" in s_chk:
        issues.append((line_no, "semicolon", s_chk[:60]))
    for w in words:
        lw = w.lower()
        if lw.endswith("ing") and len(lw) > 4 and lw not in ING_ALLOW:
            issues.append((line_no, "ing-form", w))
    for h in HEDGES:
        hm = re.search(h, s_chk, re.I)
        if hm:
            issues.append((line_no, "hedge-or-chat-abbrev", hm.group(0)))


def _split_list_groups(plines):
    groups = []
    cur = None
    cur_indent = None
    for ln, r in plines:
        lm = LIST_ITEM.match(r)
        if lm:
            indent = len(lm.group(1))
            if cur is None or cur_indent is None or indent <= cur_indent:
                if cur:
                    groups.append(cur)
                cur = (ln, [r])
                cur_indent = indent
            else:
                cur[1].append(r)
        elif cur is not None:
            cur[1].append(r)
        else:
            groups.append((ln, [r]))
    if cur:
        groups.append(cur)
    return groups


def _extract_loc_anchors(f):
    """Extract normalized (anchor, kind) tuples from a finding dict."""
    raw_locs = []
    if f.get("location"):
        raw_locs.append(str(f["location"]))
    extra = f.get("locations")
    if isinstance(extra, list):
        raw_locs.extend(str(x) for x in extra if x)
    if not raw_locs and f.get("file"):
        fpart = str(f["file"]).strip()
        lpart = str(f.get("lines") or "").strip()
        lm = re.match(r"^(\d+)", lpart)
        if fpart and lm:
            raw_locs.append(f"{fpart}:{lm.group(1)}")

    anchors = []
    for loc in raw_locs:
        s = loc.strip()
        while s.startswith(("head/", "./")):
            s = s[5:] if s.startswith("head/") else s[2:]
        m = re.match(r"^([^:\n]+:\d+)(?:-\d+)?$", s)
        if m:
            anchors.append(m.group(1))
            continue
        hm = re.match(r"^([^:\n]+?)\s+@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", s)
        if hm:
            anchors.append(f"{hm.group(1)}:{hm.group(2)}")
    return anchors


def _anchor_present(anchor, text):
    pat = r"(?<![A-Za-z0-9_./-])" + re.escape(anchor) + r"(?!\d)"
    return bool(re.search(pat, text))


def lint(text, findings=None, level=None):
    issues = []
    lines = text.splitlines()
    fence_char = None
    fence_len = 0
    in_questions = False
    par, par_start = [], 1
    paragraphs = []
    q_items = []
    cur_q = None
    cur_q_indent = 0

    def _flush_par():
        nonlocal par
        if par:
            paragraphs.append((par_start, par))
            par = []

    def _flush_q():
        nonlocal cur_q
        if cur_q:
            q_items.append(cur_q)
            cur_q = None

    for i, raw in enumerate(lines, 1):
        stripped = raw.strip()
        fm = FENCE_LINE_RX.match(raw)
        if fm:
            marker, rest = fm.group(1), fm.group(2)
            ch, mlen = marker[0], len(marker)
            if fence_char is None:
                # Single-line inline fence like ```json``` should not leave fence mode open.
                if marker in rest:
                    pass
                else:
                    fence_char, fence_len = ch, mlen
                    _flush_par()
                    _flush_q()
                    continue
            elif ch == fence_char and mlen >= fence_len and not rest.strip():
                fence_char, fence_len = None, 0
                continue
        if fence_char is not None:
            continue

        if stripped.startswith("#"):
            in_questions = bool(QUESTIONS_HEADING.match(stripped))
            _flush_par()
            _flush_q()
            vm = H2_VERDICT_RX.match(stripped)
            htext = vm.group(1) if vm else HEADING_RX.match(stripped).group(1)
            for s in sentences(clean(htext)):
                _check_sentence(s, i, issues, is_heading=True)
            continue

        if stripped.startswith(("|", "---", "<!--", "Effort:", "Level:")):
            _flush_par()
            _flush_q()
            continue

        if not stripped:
            _flush_par()
            _flush_q()
            continue

        if META_LINE_RX.match(raw) or FINDING_HEADER_RX.match(raw) or LIST_INTRO_RX.match(raw):
            _flush_par()
            if not in_questions:
                _flush_q()
            paragraphs.append((i, [(i, raw)]))
            continue

        if not par:
            par_start = i
        par.append((i, raw))

        if in_questions:
            lm = LIST_ITEM.match(raw)
            if lm:
                indent = len(lm.group(1))
                if cur_q is None or indent <= cur_q_indent:
                    _flush_q()
                    cur_q = (i, [raw])
                    cur_q_indent = indent
                else:
                    cur_q[1].append(raw)
            elif NONLIST_Q_START.match(raw):
                _flush_q()
                cur_q = (i, [raw])
                cur_q_indent = 0
            elif cur_q is not None:
                cur_q[1].append(raw)

    _flush_par()
    _flush_q()

    for item_line, item_lines in q_items:
        item_text = " ".join(x.strip() for x in item_lines)
        body = Q_LABEL_PREFIX.sub("", item_text)
        sents = sentences(clean(body))
        q_idx = next((idx for idx, s in enumerate(sents) if s.rstrip().endswith("?")), None)
        if q_idx is not None:
            if q_idx == 0:
                if not has_inline_refresher(body):
                    issues.append((item_line, "question-missing-refresher", "question has no 1-sentence context refresher"))
            elif q_idx > 1:
                issues.append((item_line, "question-refresher-over-1-sentence", f"{q_idx} sentences before question (max 1)"))

    for start, plines in paragraphs:
        first_raw = plines[0][1]
        bullets = sum(1 for _, r in plines if LIST_ITEM.match(r))
        is_middle_dot_meta = len(plines) == 1 and bool(MIDDLE_DOT_META_RX.match(first_raw))

        if is_middle_dot_meta:
            meta_clean = PREFIX.sub("", first_raw)
            for seg in meta_clean.split("·"):
                for s in sentences(clean(seg)):
                    _check_sentence(s, start, issues)
            continue

        if bullets > 0:
            item_groups = _split_list_groups(plines)
            for item_ln, item_r_list in item_groups:
                item_body = " ".join(PREFIX.sub("", r) for r in item_r_list)
                for s in sentences(clean(item_body)):
                    _check_sentence(s, item_ln, issues)
        else:
            body = " ".join(PREFIX.sub("", r) for _, r in plines)
            sents = sentences(clean(body))
            if len(sents) > 6:
                issues.append((start, "paragraph-over-6-sentences", f"{len(sents)} sentences"))
            for s in sents:
                _check_sentence(s, start, issues)
            item_groups = [(start, [r for _, r in plines])]

        for item_ln, item_r_list in item_groups:
            raw_item = " ".join(x.strip() for x in item_r_list)
            raw_no_label = Q_LABEL_PREFIX.sub("", raw_item)
            raw_sents = sentences(clean_keep_ids(raw_no_label))
            for idx, rs in enumerate(raw_sents):
                if not rs.rstrip().endswith("?"):
                    continue
                q_ids = extract_question_ids(rs)
                if not q_ids:
                    continue
                missing = False
                for qid in q_ids:
                    explained_in_prev = any(
                        re.search(rf"\b{re.escape(qid)}\b", prev_s) and _word_count_non_id(prev_s) >= 4
                        for prev_s in raw_sents[:idx]
                    )
                    if not explained_in_prev and not has_inline_refresher(rs, required_id=qid):
                        missing = True
                        break
                if missing:
                    issues.append((item_ln, "bare-id-question", rs[:80]))

    if findings:
        if isinstance(findings, dict):
            if isinstance(findings.get("shown"), list):
                flist = findings["shown"]
            else:
                flist = findings.get("findings") or []
        elif isinstance(findings, list):
            flist = findings
        else:
            flist = []
        for f in flist:
            if not isinstance(f, dict):
                continue
            if f.get("verified") == "refuted":
                continue
            if f.get("status") not in (None, "todo"):
                continue
            if f.get("eval_disposition") not in (None, "post"):
                continue
            for anchor in _extract_loc_anchors(f):
                if not _anchor_present(anchor, text):
                    issues.append((0, "evidence-lost", f"{f.get('id')} {anchor}"))
    return issues


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ste_lint.py", description=__doc__.splitlines()[0])
    ap.add_argument("file", help="Markdown report, review body, or comment file")
    ap.add_argument("--findings", help="Optional reported.json, pareto.json, or findings.json")
    ap.add_argument("--level", type=str.upper, choices=("PR", "CODEBASE"), help="Optional review level")
    ap.add_argument("--json", action="store_true", help="Print issues as JSON")
    args = ap.parse_args(argv)

    try:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write(f"ste_lint: error reading {args.file}: {exc}\n")
        return 2

    findings = None
    if args.findings:
        try:
            with open(args.findings, encoding="utf-8") as fh:
                findings = json.load(fh)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"ste_lint: error reading findings {args.findings}: {exc}\n")
            return 2

    issues = lint(text, findings, level=args.level)
    if args.json:
        print(json.dumps([{"line": a, "rule": b, "text": c} for a, b, c in issues], indent=2))
    else:
        for a, b, c in issues:
            print(f"line {a}: {b}: {c}")
        print(f"{len(issues)} issue(s)")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
