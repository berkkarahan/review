#!/usr/bin/env python3
"""Deterministic STE checks (gate G8). It does not replace the ASD-STE100 standard.

Usage: ste_lint.py <report-or-body.md> [--findings findings.json] [--json]
Checks, outside verbatim items (code spans, fences, URLs, paths, IDs, quotes):
  - sentence length: 25 words max, 20 words max for an instruction (starts with a verb in IMPERATIVE_START)
  - "-ing" words that are not in the allowlist
  - semicolons, chat abbreviations, hedges
  - paragraph length: 6 sentences max
  - evidence: with --findings, every location (path:line) of a kept finding is still in the text
Exit code: 0 clean, 1 failures (printed one per line as "line N: <rule>: <text>").
Stdlib only. Heuristics can have false positives. A human or the lead decides the edge cases.
"""
import json
import re
import sys

ING_ALLOW = {
    "pending", "string", "strings", "during", "something", "nothing", "anything", "everything",
    "opening", "remaining", "building", "thing", "things", "king", "ring", "spring", "bring",
    "morning", "evening", "ceiling", "wiring", "warning", "warnings", "heading", "headings",
    "logging", "routing", "mapping", "caching", "indexing", "pagination", "padding", "binding",
    "posting", "casting", "tracking", "encoding", "decoding", "timing", "setting", "settings",
    "ping", "being", "lighting", "hashing", "scheduling", "sibling", "siblings",
}
HEDGES = [r"\bi think\b", r"\bmaybe\b", r"\bhow about\b", r"\bi am not sure\b", r"\bi'm not sure\b",
          r"\bperhaps\b", r"\bprobably\b", r"\bit seems\b", r"\bwdyt\b", r"\bimo\b", r"\bfyi\b", r"\bimho\b"]
IMPERATIVE_START = {
    "add", "remove", "change", "write", "close", "use", "make", "move", "set", "replace", "delete",
    "keep", "wrap", "call", "check", "run", "rename", "extract", "split", "merge", "update",
    "fix", "do", "put", "return", "raise", "pass", "create", "define", "document", "test",
    "assert", "validate", "limit", "cap", "avoid", "stop", "start", "read", "load", "send",
}
FENCE = re.compile(r"```.*?```", re.S)
CODE = re.compile(r"`[^`\n]*`")
URL = re.compile(r"https?://\S+")
ID = re.compile(r"\b(?:FR|NFR|AC|B|P|OQ|AD|PD|R|C|H|M|L|Q)-?\d+[\w.-]*\b|\b[A-Z][A-Z0-9]{1,9}-\d+\b|\b[0-9a-f]{7,40}\b")
QUOTE = re.compile(r'"[^"\n]*"|“[^”\n]*”')
PATHLIKE = re.compile(r"\S+[/.]\S*\.\w+(?::\d+(?:-\d+)?)?|\S+:\d+(?:-\d+)?")
PREFIX = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+|>\s*)*(?:\[[A-Z]+ / [A-Z]+\]\s*|NIT:\s*)?")


def clean(text):
    for rx in (FENCE, CODE, URL, QUOTE):
        text = rx.sub(" X ", text)
    text = PATHLIKE.sub(" X ", text)
    text = ID.sub(" X ", text)
    return text


def sentences(par):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\[`\"(*])", par) if s.strip()]


def lint(text, findings=None):
    issues = []
    lines = text.splitlines()
    in_fence = False
    par, par_start = [], 1
    paragraphs = []
    for i, raw in enumerate(lines, 1):
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or raw.strip().startswith(("|", "#", "---", "<!--", "Effort:")):
            if par:
                paragraphs.append((par_start, par)); par = []
            continue
        if not raw.strip():
            if par:
                paragraphs.append((par_start, par)); par = []
            continue
        if not par:
            par_start = i
        par.append((i, raw))
    if par:
        paragraphs.append((par_start, par))

    for start, plines in paragraphs:
        body = " ".join(PREFIX.sub("", r) for _, r in plines)
        sents = sentences(clean(body))
        bullets = sum(1 for _, r in plines if re.match(r"^\s*(?:>\s*)?(?:[-*]|\d+\.)\s", r))
        if len(sents) > 6 and bullets == 0:
            issues.append((start, "paragraph-over-6-sentences", f"{len(sents)} sentences"))
        for s in sents:
            words = re.findall(r"[A-Za-z][A-Za-z'-]*|\d+|X", s)
            first = words[0].lower() if words else ""
            limit = 20 if first in IMPERATIVE_START else 25
            if len(words) > limit:
                issues.append((start, f"sentence-over-{limit}-words", " ".join(words[:12]) + " ..."))
            if ";" in s:
                issues.append((start, "semicolon", s[:60]))
            for w in words:
                lw = w.lower()
                if lw.endswith("ing") and len(lw) > 4 and lw not in ING_ALLOW:
                    issues.append((start, "ing-form", w))
            for h in HEDGES:
                if re.search(h, s, re.I):
                    issues.append((start, "hedge-or-chat-abbrev", re.search(h, s, re.I).group(0)))
    if findings:
        flist = findings.get("findings", []) if isinstance(findings, dict) else findings
        for f in flist:
            locs = [f.get("location", "")] + list(f.get("locations", []))
            for loc in locs:
                m = re.match(r"^([^\s:]+:\d+)", str(loc))
                if m and m.group(1) not in text and f.get("verified") != "refuted":
                    issues.append((0, "evidence-lost", f"{f.get('id')} {m.group(1)}"))
    return issues


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit("usage: ste_lint.py <file> [--findings findings.json] [--json]")
    path, findings, as_json = args[0], None, "--json" in args
    if "--findings" in args:
        findings = json.load(open(args[args.index("--findings") + 1], encoding="utf-8"))
    issues = lint(open(path, encoding="utf-8").read(), findings)
    if as_json:
        print(json.dumps([{"line": a, "rule": b, "text": c} for a, b, c in issues], indent=2))
    else:
        for a, b, c in issues:
            print(f"line {a}: {b}: {c}")
        print(f"{len(issues)} issue(s)")
    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
