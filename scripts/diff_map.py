#!/usr/bin/env python3
"""Build a right-side line map from a unified diff.

Usage: diff_map.py <diff.patch> > diff-map.json
Output: {"<path>": {"added": [lines], "context": [lines], "hunks": [...]}}
Used by: gate G2 (in-diff check, Phase 2.2) and Posting step 2 (inline anchors).
Stdlib only. Never executes anything from the diff.
"""
import argparse
import json
import re
import sys

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_ESCAPE_BYTES = {
    "a": b"\a",
    "b": b"\b",
    "t": b"\t",
    "n": b"\n",
    "v": b"\v",
    "f": b"\f",
    "r": b"\r",
    '"': b'"',
    "\\": b"\\",
}


def unquote_git_path(token):
    """Unquote a C-escaped git diff path token, returning UTF-8 text."""
    if not token or len(token) < 2 or token[0] != '"' or token[-1] != '"':
        return token
    inner = token[1:-1]
    buf = bytearray()
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\" and i + 1 < n:
            nxt = inner[i + 1]
            if nxt in _ESCAPE_BYTES:
                buf.extend(_ESCAPE_BYTES[nxt])
                i += 2
                continue
            if i + 3 < n and all(c in "01234567" for c in inner[i + 1:i + 4]):
                buf.append(int(inner[i + 1:i + 4], 8))
                i += 4
                continue
            buf.extend(nxt.encode("utf-8"))
            i += 2
            continue
        buf.extend(ch.encode("utf-8"))
        i += 1
    return buf.decode("utf-8", errors="replace")


def strip_diff_prefix(path, prefix):
    """Strip git's leading a/ or b/ prefix unless path is /dev/null."""
    if path == "/dev/null":
        return path
    return path[len(prefix):] if path.startswith(prefix) else path


def parse_header_path(raw_tail):
    """Parse a single path tail from ---/+++/rename/copy diff header lines."""
    s = raw_tail.rstrip("\r\n")
    if s.startswith('"'):
        i = 1
        n = len(s)
        while i < n:
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == '"':
                return unquote_git_path(s[:i + 1])
            i += 1
        return s
    if "\t" in s:
        s = s.split("\t", 1)[0]
    return s


def parse_diff_git_paths(line):
    """Extract (old_path, new_path) from a `diff --git` header line."""
    if not line.startswith("diff --git "):
        return "", ""
    rest = line[len("diff --git "):].rstrip("\r\n")
    if not rest:
        return "", ""

    old_tok = ""
    new_tok = ""
    if rest.startswith('"'):
        i = 1
        n = len(rest)
        while i < n:
            if rest[i] == "\\":
                i += 2
                continue
            if rest[i] == '"':
                old_tok = unquote_git_path(rest[:i + 1])
                tail = rest[i + 1:].lstrip(" ")
                new_tok = parse_header_path(tail) if tail.startswith('"') else tail
                break
            i += 1
    elif ' "b/' in rest:
        idx = rest.rfind(' "b/')
        old_tok = rest[:idx]
        new_tok = parse_header_path(rest[idx + 1:])
    else:
        if (len(rest) - 1) % 2 == 0:
            mid = (len(rest) - 1) // 2
            if (
                mid > 1
                and rest[mid] == " "
                and rest[:mid].startswith("a/")
                and rest[mid + 1:].startswith("b/")
                and rest[:mid][2:] == rest[mid + 1:][2:]
            ):
                p = rest[:mid][2:]
                return p, p
        m = re.match(r"^a/(.+?) b/(.+)$", rest)
        if m:
            return m.group(1), m.group(2)
        return "", ""

    return strip_diff_prefix(old_tok, "a/"), strip_diff_prefix(new_tok, "b/")


def parse_hunk_header(line):
    """Parse `@@ -a[,b] +c[,d] @@` into (old_start, old_count, new_start, new_count)."""
    m = HUNK.match(line)
    if not m:
        return None
    old_start = int(m.group(1))
    old_count = int(m.group(2)) if m.group(2) is not None else 1
    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) is not None else 1
    return old_start, old_count, new_start, new_count


def build(text):
    out = {}
    path = None
    new_line = 0
    old_rem = 0
    new_rem = 0
    in_header = True

    for raw in text.splitlines():
        if old_rem > 0 or new_rem > 0:
            if raw.startswith("+"):
                if path:
                    out[path]["added"].append(new_line)
                new_line += 1
                if new_rem > 0:
                    new_rem -= 1
            elif raw.startswith("-"):
                if old_rem > 0:
                    old_rem -= 1
            elif raw.startswith("\\"):
                continue
            else:
                if path:
                    out[path]["context"].append(new_line)
                new_line += 1
                if old_rem > 0:
                    old_rem -= 1
                if new_rem > 0:
                    new_rem -= 1
            continue

        if raw.startswith("diff --git "):
            path = None
            new_line = 0
            in_header = True
        elif raw.startswith("--- "):
            path = None
            new_line = 0
            in_header = True
        elif in_header and raw.startswith("+++ "):
            tgt = strip_diff_prefix(parse_header_path(raw[4:]), "b/")
            path = None if (not tgt or tgt == "/dev/null") else tgt
            new_line = 0
            if path:
                out.setdefault(path, {"added": [], "context": [], "hunks": []})
        elif raw.startswith("@@") and path:
            hm = parse_hunk_header(raw)
            if hm:
                old_start, old_count, new_start, new_count = hm
                in_header = False
                old_rem = old_count
                new_rem = new_count
                new_line = new_start
                out[path]["hunks"].append({
                    "old_start": old_start,
                    "old_count": old_count,
                    "new_start": new_start,
                    "new_count": new_count,
                })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="diff_map.py", description=__doc__.splitlines()[0])
    ap.add_argument("diff_patch", help="Path to unified diff patch file")
    args = ap.parse_args(argv)
    try:
        with open(args.diff_patch, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write(f"diff_map: error: {exc}\n")
        return 2
    json.dump(build(text), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
