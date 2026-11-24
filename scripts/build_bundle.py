#!/usr/bin/env python3
"""Parallel-threaded Phase 0 bundle builder, diff/codebase slicer, signal/effort resolver, and carry preparer.

Usage:
  build_bundle.py [target] [--level PR|CODEBASE] [--cwd <dir>] [--bundle-dir <dir>]
                  [--effort auto|high|medium|low|skim] [--since-last] [--fresh]
                  [--lanes a,b] [--no-inherit] [--inline] [--no-tier]
                  [--spec <path-or-url>]... [--persona <path>] [--reviews-dir <dir>]
                  [--local-only] [--shard-max-files <n>] [--shard-max-loc <n>]

Outputs a compact JSON summary to stdout and writes `<bundle_dir>/bundle-summary.json`.
Stdlib only. All shell identifiers are strictly sanitized before any git/gh command (shell=False).
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import carry_findings  # noqa: E402
import diff_map  # noqa: E402

SAFE_IDENT = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_NUM = re.compile(r"^[0-9]+$")
SAFE_SHA = re.compile(r"^[0-9a-f]{40}$")
UNSAFE_SHELL = re.compile(r'[\'"`$;|&<>\\\n\r]')

PR_URL_RX = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)/pull/([0-9]+)(?:[/?#].*)?$"
)
PR_SHORTHAND_RX = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)$")
PR_NUM_RX = re.compile(r"^#?([0-9]+)$")

TRACKER_KEY_RX = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
NON_TICKET_PREFIXES = (
    "UTF-", "ISO-", "SHA-", "RFC-", "HTTP-", "TLS-", "CVE-",
    "FR-", "NFR-", "AC-", "AD-", "OQ-", "PD-", "R-", "C-", "H-", "M-", "L-", "Q-",
)
GH_ISSUE_URL_RX = re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+")
GH_ISSUE_SHORTHAND_RX = re.compile(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)\b")
GH_ISSUE_LOCAL_RX = re.compile(r"(?<![A-Za-z0-9_#/&.-])#([0-9]+)\b")
URL_RX = re.compile(r"https?://[^\s)>\]\"']+")

RULE_BASENAMES = {
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", "CONTRIBUTING.md",
    "ARCHITECTURE.md", "STYLEGUIDE.md", ".cursorrules", ".windsurfrules", ".clinerules",
}
RULE_PREFIXES = (".cursor/rules/", ".claude/rules/", ".github/instructions/")
RULE_EXACT = {".github/copilot-instructions.md"}

SPEC_PREFIXES = (
    "docs/specs/", "docs/design/", "docs/rfcs/", "docs/adr/", "docs/prd/", "docs/stories/",
    "specs/", "plans/", "tasks/", ".kiro/specs/", ".specify/", "openspec/", "_bmad/",
    ".cursor/plans/", ".claude/plans/", ".gemini/",
)
SPEC_BASENAMES = {
    "plan.md", "implementation_plan.md", "IMPLEMENTATION_PLAN.md", "PLAN.md",
    "design.md", "requirements.md", "tasks.md", "prd.md", "PRD.md",
}
SPEC_EXTS = (".md", ".mdc", ".txt", ".yaml", ".yml", ".rst", ".adoc")

LOCKFILE_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "Pipfile.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum", "uv.lock", "bun.lockb", "bun.lock",
}
LOCKFILE_NAMES_LOWER = {name.lower() for name in LOCKFILE_NAMES}
MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile",
    "pom.xml", "composer.json", "requirements.txt",
}
MANIFEST_NAMES_LOWER = {name.lower() for name in MANIFEST_NAMES}
SECRET_BASENAMES_LOWER = {"id_rsa", "id_ed25519", "credentials.json"}

DOC_EXTS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".eot", ".pdf", ".mp4", ".mp3", ".zip", ".gz", ".tar",
}
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts",
    ".go", ".rs", ".java", ".kt", ".kts", ".rb", ".erb", ".php", ".cs",
    ".cpp", ".cc", ".c", ".h", ".hpp", ".m", ".mm", ".swift", ".scala",
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".vue", ".svelte", ".astro",
    ".dart", ".ex", ".exs", ".erl", ".hs", ".clj", ".lua", ".pl", ".r",
    ".tf", ".hcl", ".groovy", ".zig", ".nim", ".jl",
}
LANG_MAP = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".rb": "ruby", ".erb": "ruby", ".php": "php", ".cs": "csharp", ".cpp": "cpp", ".cc": "cpp",
    ".c": "c", ".h": "c", ".hpp": "cpp", ".m": "objc", ".mm": "objcpp", ".swift": "swift",
    ".scala": "scala", ".sql": "sql", ".vue": "vue", ".svelte": "svelte", ".astro": "astro",
    ".dart": "dart", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell",
    ".clj": "clojure", ".lua": "lua", ".pl": "perl", ".r": "r", ".tf": "terraform", ".hcl": "hcl",
    ".groovy": "groovy", ".zig": "zig", ".nim": "nim", ".jl": "julia", ".md": "markdown",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".css": "css", ".scss": "scss", ".html": "html",
}

SIGNAL_RULES = {
    "auth": re.compile(
        r"\b(auth|authentication|authorization|authorisation|permission|policy|guard|middleware|"
        r"scope|token|jwt|oauth|session|role|acl|login_required|Authorize|PreAuthorize|before_action)\b",
        re.I | re.M,
    ),
    "input": re.compile(
        r"(controller|handler|serializer|dto|validator|upload|webhook|deseriali[sz]|raw_sql|"
        r"request\.body|req\.query|request\.data|params\[|SELECT\s+[^\n]+\+|execute\s*\()",
        re.I | re.M,
    ),
    "deps": re.compile(
        r"(package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|requirements[^\n/]*\.txt|"
        r"pyproject\.toml|go\.mod|go\.sum|Cargo\.toml|Cargo\.lock|Gemfile|pom\.xml|"
        r"build\.gradle|composer\.json|\.csproj|Dockerfile|\.github/workflows/)",
        re.I | re.M,
    ),
    "data": re.compile(
        r"((?:^|/)migrations?/|(?:^|/)db/migrate/|\.sql$|flyway|liquibase|\bmodels?\b|\bentities?\b|"
        r"\bALTER\s+TABLE\b|\bDROP\s+TABLE\b|bulk_update|bulk_delete|backfill)",
        re.I | re.M,
    ),
    "contract": re.compile(
        r"((?:^|/)routes?/|router|openapi|swagger|graphql|\.proto$|\.avsc$|(?:^|/)v[12]/|(?:^|/)public/|"
        r"serializer|schema|event_schema|feature_flag|featureFlag)",
        re.I | re.M,
    ),
    "perf": re.compile(
        r"(pagination|paginate|cache|redis|memcached|worker|background|batch|prefetch|"
        r"select_related|N\+1|useMemo|useCallback|\bfor\s+[^\n:]{1,120}:[^\n]{0,200}(?:query|fetch|http|requests\.))",
        re.I | re.M,
    ),
    "async": re.compile(
        r"(\basync\b|\bawait\b|goroutine|\bgo\s+\w+|threading|mutex|\block\b|semaphore|"
        r"celery|sidekiq|bullmq|kafka|rabbitmq|transaction|on_commit|after_commit|retry|backoff|idempoten)",
        re.I | re.M,
    ),
    "ui": re.compile(
        r"(\.tsx$|\.jsx$|\.vue$|\.svelte$|\.css$|\.scss$|(?:^|/)components?/|(?:^|/)views?/|(?:^|/)screens?/)",
        re.I | re.M,
    ),
}

REVIEW_LEVELS = ("PR", "CODEBASE")
VALID_LANES = {
    "adherence", "business-logic", "code-quality", "conventions",
    "security", "performance", "compatibility", "robustness", "critical-risk", "skim",
}
MICRO_FOLD_MAX_LINES = 40
MICRO_FOLD_MAX_FILES = 2
MICRO_FOLDABLE_LANES = ("performance", "compatibility", "robustness")
CODEBASE_SHARD_MAX_FILES = 30
CODEBASE_SHARD_MAX_LOC = 6000
CODEBASE_SCAN_MAX_BYTES = 262144
WALK_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".next", "target", ".idea", ".vscode",
}
CODEBASE_DOC_BASENAMES = {
    "README.md", "ARCHITECTURE.md", "DESIGN.md", "CONTRIBUTING.md", "SECURITY.md",
}
KNOWN_BOT_LOGINS = {
    "dependabot", "renovate", "github-actions", "codecov", "sonarcloud",
    "mergify", "snyk-bot", "pre-commit-ci",
}
DEFAULT_REVIEWS_DIR = os.path.expanduser("~/.claude/review/reviews")
LEGACY_REVIEWS_DIR = os.path.expanduser("~/.claude/generic-pr-review/reviews")


# ---------------------------------------------------------------------------
# Process, path, and file classifiers
# ---------------------------------------------------------------------------

def run_cmd(argv, cwd=None, timeout=60):
    try:
        p = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, shell=False,
        )
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def is_safe_relpath(p):
    if not p or "\x00" in p or p.startswith("/") or p.endswith("/") or "\\" in p:
        return False
    parts = p.split("/")
    return "" not in parts and ".." not in parts and "." not in parts


def is_lockfile(path):
    low_base = os.path.basename(path).lower()
    return low_base in LOCKFILE_NAMES_LOWER or low_base.endswith(".lock")


def is_secret_or_vendor(path):
    low = path.lower()
    low_base = os.path.basename(low)
    parts = low.split("/")
    if low_base.startswith(".env") or low.endswith(".pem") or low_base in SECRET_BASENAMES_LOWER:
        return True
    if "node_modules" in parts:
        return True
    _, ext = os.path.splitext(low)
    return ext in ASSET_EXTS


def is_denylisted(path):
    return is_secret_or_vendor(path) or is_lockfile(path)


def is_test_path(path):
    base = os.path.basename(path)
    low = path.lower()
    if any(seg in low.split("/") for seg in ("tests", "test", "__tests__", "__snapshots__")):
        return True
    return bool(
        re.search(r"\.(?:spec|test)\.[^/]+$", base, re.I)
        or base.lower().startswith("test_")
        or re.search(r"_test\.[^/]+$", base, re.I)
        or base.lower().endswith(".snap")
    )


def is_generated_or_locale(path):
    low = path.lower()
    parts = low.split("/")
    if any(seg in parts for seg in ("generated", "locale", "locales", "__snapshots__")):
        return True
    return low.endswith((".po", ".min.js", ".min.css", ".snap"))


def is_lock_or_manifest(path):
    low_base = os.path.basename(path).lower()
    return (
        is_lockfile(path)
        or low_base in MANIFEST_NAMES_LOWER
        or bool(re.match(r"^requirements.*\.txt$", low_base))
        or low_base.startswith("build.gradle")
        or low_base.endswith(".csproj")
    )


def is_rule_file(path):
    base = os.path.basename(path)
    low_base = base.lower()
    if base in RULE_BASENAMES or path in RULE_EXACT or any(path.endswith("/" + ex) for ex in RULE_EXACT):
        return True
    if any(path.startswith(pref) or f"/{pref}" in path for pref in RULE_PREFIXES):
        return True
    return "security" in low_base and "checklist" in low_base


def is_spec_file(path):
    base = os.path.basename(path)
    low = path.lower()
    if base in SPEC_BASENAMES or low.endswith((".spec.md", ".plan.md")):
        return True
    if not low.endswith(SPEC_EXTS):
        return False
    return any(path.startswith(pref) or f"/{pref}" in path for pref in SPEC_PREFIXES)


def is_executable_code(path):
    _, ext = os.path.splitext(path.lower())
    return ext in CODE_EXTS


def is_doc_or_asset(path):
    _, ext = os.path.splitext(path.lower())
    return ext in DOC_EXTS or ext in ASSET_EXTS


def lang_of(path):
    _, ext = os.path.splitext(path.lower())
    return LANG_MAP.get(ext, ext.lstrip(".") or "other")


# ---------------------------------------------------------------------------
# Diff and persona parsing
# ---------------------------------------------------------------------------

def parse_diff_by_file(diff_text):
    """Split a unified diff into per-file sections with status and +/- line counts."""
    sections = []
    cur = None
    old_rem = 0
    new_rem = 0
    in_header = True

    for line in diff_text.splitlines(keepends=True):
        if cur is not None and (old_rem > 0 or new_rem > 0):
            cur["lines"].append(line)
            if line.startswith("+"):
                cur["adds"] += 1
                if new_rem > 0:
                    new_rem -= 1
            elif line.startswith("-"):
                cur["dels"] += 1
                if old_rem > 0:
                    old_rem -= 1
            elif line.startswith("\\"):
                continue
            else:
                if old_rem > 0:
                    old_rem -= 1
                if new_rem > 0:
                    new_rem -= 1
            continue

        if line.startswith("diff --git "):
            if cur:
                sections.append(cur)
            old_p, new_p = diff_map.parse_diff_git_paths(line)
            cur = {
                "old_path": old_p,
                "path": new_p,
                "status": "modified",
                "adds": 0,
                "dels": 0,
                "binary": False,
                "lines": [line],
            }
            old_rem = 0
            new_rem = 0
            in_header = True
            continue
        if cur is None:
            continue
        cur["lines"].append(line)
        if in_header and not cur["binary"]:
            if line.startswith("new file mode "):
                cur["status"] = "added"
            elif line.startswith("deleted file mode "):
                cur["status"] = "deleted"
            elif line.startswith("rename from "):
                cur["status"] = "renamed"
                cur["old_path"] = diff_map.parse_header_path(line[len("rename from "):])
            elif line.startswith("rename to "):
                cur["path"] = diff_map.parse_header_path(line[len("rename to "):])
            elif line.startswith("copy from "):
                cur["status"] = "added"
                cur["old_path"] = diff_map.parse_header_path(line[len("copy from "):])
            elif line.startswith("copy to "):
                cur["path"] = diff_map.parse_header_path(line[len("copy to "):])
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur["binary"] = True
            elif line.startswith("--- "):
                src = diff_map.strip_diff_prefix(diff_map.parse_header_path(line[4:]), "a/")
                if src and src != "/dev/null":
                    cur["old_path"] = src
            elif line.startswith("+++ "):
                tgt = diff_map.strip_diff_prefix(diff_map.parse_header_path(line[4:]), "b/")
                if tgt == "/dev/null":
                    cur["status"] = "deleted"
                elif tgt:
                    cur["path"] = tgt
            elif line.startswith("@@"):
                hm = diff_map.parse_hunk_header(line)
                if hm:
                    _, old_rem, _, new_rem = hm
                    in_header = False
        elif not cur["binary"] and line.startswith("@@"):
            hm = diff_map.parse_hunk_header(line)
            if hm:
                _, old_rem, _, new_rem = hm
    if cur:
        sections.append(cur)
    for s in sections:
        s["text"] = "".join(s["lines"])
        s["lang"] = lang_of(s["path"])
        del s["lines"]
    return sections


def _extract_changed_lines(diff_section_text):
    """Extract added and removed content lines strictly inside diff hunks."""
    out = []
    old_rem = 0
    new_rem = 0
    for raw in diff_section_text.splitlines():
        if old_rem > 0 or new_rem > 0:
            if raw.startswith("+"):
                out.append(raw[1:])
                if new_rem > 0:
                    new_rem -= 1
            elif raw.startswith("-"):
                out.append(raw[1:])
                if old_rem > 0:
                    old_rem -= 1
            elif raw.startswith("\\"):
                continue
            else:
                if old_rem > 0:
                    old_rem -= 1
                if new_rem > 0:
                    new_rem -= 1
            continue
        if raw.startswith("@@"):
            hm = diff_map.parse_hunk_header(raw)
            if hm:
                _, old_rem, _, new_rem = hm
    return "\n".join(out)


def _strip_yaml_comment(raw):
    """Strip an unquoted `# ...` comment from a YAML frontmatter line."""
    quote = None
    escape = False
    for idx, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == "\\" and quote == '"':
            escape = True
            continue
        if ch in ("'", '"'):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
        elif ch == "#" and quote is None:
            return raw[:idx].rstrip()
    return raw.rstrip()


def parse_persona_frontmatter(persona_path):
    """Parse persona.md frontmatter + detect active §8/§9 rules without external YAML libs."""
    info = {
        "status": "empty", "refreshed": None, "memory": "ask",
        "emphasis": [], "has_house_rules": False, "has_learned_prefs": False,
        "voice": {
            "tone": "direct",
            "spelling": "american",
            "max_inline": 8,
            "hold_event": "REQUEST_CHANGES",
            "comment_prefix": "[{SEVERITY}]",
            "nit_prefix": "Nit:",
            "emoji": "none",
        },
    }
    if not persona_path or not os.path.isfile(persona_path):
        return info
    with open(persona_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if m:
        fm, body = m.group(1), m.group(2)
        in_emphasis = False
        cur_section = None
        for raw in fm.splitlines():
            line = _strip_yaml_comment(raw)
            if not line.strip():
                continue
            if re.match(r"^emphasis:\s*\[(.*)\]\s*$", line):
                items = [x.strip().strip("'\"") for x in re.match(r"^emphasis:\s*\[(.*)\]", line).group(1).split(",") if x.strip()]
                info["emphasis"] = items
                in_emphasis = False
                cur_section = None
            elif re.match(r"^emphasis:\s*$", line):
                in_emphasis = True
                cur_section = "emphasis"
            elif in_emphasis and re.match(r"^\s*-\s+(.+)$", line):
                info["emphasis"].append(re.match(r"^\s*-\s+(.+)$", line).group(1).strip().strip("'\""))
            else:
                in_emphasis = False
                sec_hdr = re.match(r"^([A-Za-z0-9_]+):\s*$", line)
                if sec_hdr:
                    cur_section = sec_hdr.group(1)
                    continue
                kv = re.match(r"^(\s*)([A-Za-z0-9_]+):\s*(.+?)\s*$", line)
                if kv:
                    indent, k, v = len(kv.group(1)), kv.group(2), kv.group(3).strip("'\"")
                    if indent == 0:
                        cur_section = None
                        if k == "status":
                            info["status"] = v
                        elif k == "refreshed":
                            info["refreshed"] = None if v == "null" else v
                        elif k == "memory":
                            info["memory"] = v
                    elif cur_section in (None, "voice"):
                        if k == "tone":
                            info["voice"]["tone"] = v
                        elif k == "spelling":
                            info["voice"]["spelling"] = v
                        elif k == "max_inline" and v.isdigit():
                            info["voice"]["max_inline"] = int(v)
                        elif k == "hold_event":
                            info["voice"]["hold_event"] = v
                        elif k == "comment_prefix":
                            info["voice"]["comment_prefix"] = v
                        elif k == "nit_prefix":
                            info["voice"]["nit_prefix"] = v
                        elif k == "emoji":
                            info["voice"]["emoji"] = v
    else:
        body = text

    sec8 = re.search(r"##\s*8\.\s*House rules.*?(?=\n##\s*9\.|\Z)", body, re.S | re.I)
    if sec8:
        content8 = re.sub(r"```.*?```", "", sec8.group(0), flags=re.S)
        info["has_house_rules"] = bool(re.search(r"^\d+\.\s+\S", content8, re.M))
    sec9 = re.search(r"##\s*9\.\s*Learned preferences.*", body, re.S | re.I)
    if sec9:
        content9 = re.sub(r"```.*?```", "", sec9.group(0), flags=re.S)
        info["has_learned_prefs"] = bool(re.search(r"^\d+\.\s+\[", content9, re.M))
    return info


def _persona_summary(persona):
    return {
        "status": persona["status"],
        "refreshed": persona["refreshed"],
        "memory": persona["memory"],
        "emphasis": list(persona.get("emphasis") or []),
        "voice": persona["voice"],
        "has_rules": bool(persona["has_house_rules"] or persona["has_learned_prefs"]),
    }


# ---------------------------------------------------------------------------
# Target, clone, GitHub API, CI, and snapshot helpers
# ---------------------------------------------------------------------------

def _parse_github_remote(url):
    rm = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", str(url or "").strip())
    if not rm:
        return None, None
    owner, repo = rm.group(1), rm.group(2)
    if owner in (".", "..") or repo in (".", ".."):
        return None, None
    return owner, repo


def find_clone(cwd, owner=None, repo=None):
    candidates = []
    rc, top, _ = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if rc == 0 and top.strip():
        candidates.append(top.strip())
    if repo and SAFE_IDENT.match(repo) and repo not in (".", ".."):
        candidates.append(os.path.join(cwd, repo))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(cwd)), repo))
    exact_rx = None
    if owner and repo:
        exact_rx = re.compile(rf"(?:[:/]){re.escape(owner)}/{re.escape(repo)}(?:\.git)?/?$", re.I)
    for cand in candidates:
        if not os.path.isdir(cand):
            continue
        if not owner or not repo:
            return cand
        rc, url, _ = run_cmd(["git", "-C", cand, "remote", "get-url", "origin"])
        if rc == 0 and exact_rx.search(url.strip()):
            return cand
    return None


def _validated_pr_coords(owner, repo, num):
    if (
        owner in (".", "..") or repo in (".", "..")
        or not SAFE_IDENT.match(owner) or not SAFE_IDENT.match(repo) or not SAFE_NUM.match(num)
    ):
        raise ValueError("invalid owner, repo, or pull request number")
    return "pr", owner, repo, num, f"https://github.com/{owner}/{repo}/pull/{num}"


def parse_target(target, cwd):
    """Return (mode, owner, repo, number, url) or raise ValueError on unsafe input."""
    if target is None or not str(target).strip():
        return "auto", None, None, None, None
    t = str(target).strip()
    if UNSAFE_SHELL.search(t):
        raise ValueError("unsafe characters in target argument")
    m = PR_URL_RX.match(t)
    if m:
        return _validated_pr_coords(m.group(1), m.group(2), m.group(3))
    m = PR_SHORTHAND_RX.match(t)
    if m:
        return _validated_pr_coords(m.group(1), m.group(2), m.group(3))
    m = PR_NUM_RX.match(t)
    if m:
        num = m.group(1)
        rc, url, _ = run_cmd(["git", "remote", "get-url", "origin"], cwd=cwd)
        if rc != 0:
            raise ValueError("cannot infer owner/repo for #N without git origin")
        owner, repo = _parse_github_remote(url)
        if not owner or not repo:
            raise ValueError("origin remote is not a GitHub repository")
        return _validated_pr_coords(owner, repo, num)
    raise ValueError(f"unrecognized target format: {t}")


def compute_ci(rollup):
    if not rollup:
        return "none"
    failing = []
    pending = False
    for item in rollup:
        if not isinstance(item, dict):
            continue
        concl = str(item.get("conclusion") or "").upper()
        state = str(item.get("state") or item.get("status") or "").upper()
        name = item.get("name") or item.get("context") or "check"
        if concl in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE") or state in ("ERROR", "FAILURE"):
            failing.append(name)
        elif (
            state in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING")
            or concl == "STALE"
            or (not concl and state != "SUCCESS")
        ):
            pending = True
    if failing:
        return "failing: " + ", ".join(failing[:5])
    if pending:
        return "pending"
    return "passing"


def parse_paginated_json_list(raw_text):
    """Parse concatenated JSON arrays or NDJSON objects emitted by `gh api --paginate`."""
    text = (raw_text or "").strip()
    if not text:
        return [], True
    decoder = json.JSONDecoder()
    items = []
    pos = 0
    n = len(text)
    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except ValueError:
            return [], False
        if isinstance(obj, list):
            items.extend(x for x in obj if isinstance(x, dict))
        elif isinstance(obj, dict):
            items.append(obj)
        pos = end
    return items, True


def is_bot_author(author_obj):
    if not isinstance(author_obj, dict):
        return False
    login = str(author_obj.get("login") or "").lower()
    return bool(author_obj.get("is_bot")) or (
        login in KNOWN_BOT_LOGINS or login.endswith(("[bot]", "-bot", "_bot"))
    )


def sanitize_pr_data(pr_data):
    """Strip commit author names/emails from pr.json while preserving headlines and author login/bot flag."""
    if not isinstance(pr_data, dict):
        return pr_data
    clean = dict(pr_data)
    if isinstance(clean.get("author"), dict):
        author = clean["author"]
        clean["author"] = {k: author[k] for k in ("login", "is_bot") if k in author}
    if isinstance(clean.get("commits"), list):
        clean_commits = []
        for c in clean["commits"]:
            if isinstance(c, dict):
                entry = {}
                if "messageHeadline" in c:
                    entry["messageHeadline"] = c["messageHeadline"]
                if "oid" in c:
                    entry["oid"] = c["oid"]
                clean_commits.append(entry)
            else:
                clean_commits.append(c)
        clean["commits"] = clean_commits
    return clean


def _resolve_bundle_dir(bundle_dir_arg, owner, repo, identifier, sha7):
    if bundle_dir_arg:
        bundle_dir = os.path.abspath(bundle_dir_arg)
    else:
        job_dir = os.environ.get("CLAUDE_JOB_DIR")
        base_tmp = os.path.join(job_dir, "tmp") if job_dir else tempfile.gettempdir()
        bundle_dir = os.path.join(base_tmp, "review", f"{owner}-{repo}-{identifier}-{sha7}")
    os.makedirs(bundle_dir, exist_ok=True)
    return bundle_dir


def copy_snapshot_files(clone, head_sha, paths, out_root, owner=None, repo=None):
    """Copy repo files at head_sha into out_root in parallel threads."""
    if not SAFE_SHA.match(str(head_sha or "")):
        return [], list(paths)
    copied = []
    failed = []

    def fetch_one(path):
        if not is_safe_relpath(path) or is_denylisted(path):
            return path, False
        dst = os.path.join(out_root, path)
        if clone:
            rc, content, _ = run_cmd(["git", "-C", clone, "show", f"{head_sha}:{path}"])
        elif owner and repo:
            qpath = urllib.parse.quote(path, safe="/")
            rc, content, _ = run_cmd([
                "gh", "api", f"repos/{owner}/{repo}/contents/{qpath}?ref={head_sha}",
                "-H", "Accept: application/vnd.github.raw",
            ])
        else:
            return path, False
        if rc != 0:
            return path, False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(content)
        return path, True

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for p, ok in ex.map(fetch_one, paths):
            (copied if ok else failed).append(p)
    return copied, failed


def find_context_files(clone, head_sha, changed_paths, max_files, max_loc):
    if not clone or not head_sha or not SAFE_SHA.match(head_sha):
        return []
    changed_set = set(changed_paths)
    scores = {}

    def grep_one(path):
        base = os.path.splitext(os.path.basename(path))[0]
        if len(base) < 3 or base in ("index", "main", "init", "__init__", "utils", "types"):
            return []
        found = []
        rc, out, _ = run_cmd(["git", "-C", clone, "grep", "-l", "-F", "-e", base, head_sha])
        if rc == 0:
            for m in out.splitlines():
                if ":" in m:
                    m = m.split(":", 1)[1]
                if (
                    m not in changed_set
                    and is_safe_relpath(m)
                    and not is_denylisted(m)
                    and not is_generated_or_locale(m)
                ):
                    found.append(m)
        return found

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for hits in ex.map(grep_one, changed_paths[:20]):
            for h in hits:
                scores[h] = scores.get(h, 0) + 1

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    selected = []
    total_loc = 0
    for path, _ in ranked:
        if len(selected) >= max_files:
            break
        rc, content, _ = run_cmd(["git", "-C", clone, "show", f"{head_sha}:{path}"])
        if rc != 0 or "\x00" in content[:8192]:
            continue
        loc = content.count("\n") + 1
        if total_loc + loc > max_loc:
            continue
        selected.append((path, content))
        total_loc += loc
    return selected


# ---------------------------------------------------------------------------
# Signals, slicing, sharding, and lane selection
# ---------------------------------------------------------------------------

def detect_signals(sections):
    signals = {}
    for sec in sections:
        path = sec["path"]
        if is_doc_or_asset(path) and not is_lock_or_manifest(path):
            continue
        changed = _extract_changed_lines(sec["text"])
        haystack = path + "\n" + changed
        for sig, rx in SIGNAL_RULES.items():
            if rx.search(haystack):
                signals.setdefault(sig, []).append(path)
    return signals


def slice_lanes(sections, signals):
    """Build per-lane sliced diff text and changed-line counts."""
    sig_sets = {k: set(v) for k, v in signals.items()}
    by_lane = {
        "adherence": [], "code-quality": [], "conventions": [],
        "security": [], "performance": [], "compatibility": [], "robustness": [],
    }
    for sec in sections:
        p = sec["path"]
        if sec["binary"] or is_secret_or_vendor(p):
            continue
        is_lock = is_lock_or_manifest(p)
        is_gen = is_generated_or_locale(p)
        is_test = is_test_path(p)
        is_code = is_executable_code(p)
        is_rule = is_rule_file(p)
        is_doc = is_doc_or_asset(p)

        if not is_lock and not is_gen:
            by_lane["adherence"].append(sec)
        if is_code and not is_gen:
            by_lane["code-quality"].append(sec)
        if ((is_code and not is_test) or is_rule) and not is_gen:
            by_lane["conventions"].append(sec)
        if p in sig_sets.get("auth", set()) or p in sig_sets.get("input", set()) or p in sig_sets.get("deps", set()) or is_lock:
            by_lane["security"].append(sec)
        if (p in sig_sets.get("perf", set()) or p in sig_sets.get("data", set()) or p in sig_sets.get("ui", set())) and not is_test and not is_doc and not is_gen:
            by_lane["performance"].append(sec)
        if (p in sig_sets.get("contract", set()) or p in sig_sets.get("data", set())) and not is_test and not is_gen:
            by_lane["compatibility"].append(sec)
        if (p in sig_sets.get("async", set()) or p in sig_sets.get("data", set())) and not is_test and not is_doc and not is_gen:
            by_lane["robustness"].append(sec)

    manifest = {}
    patches = {}
    for lane, secs in by_lane.items():
        lines = sum(s["adds"] + s["dels"] for s in secs)
        manifest[lane] = {
            "files": [s["path"] for s in secs],
            "file_count": len(secs),
            "changed_lines": lines,
            "patch": f"lanes/{lane}.patch",
        }
        patches[lane] = "".join(s["text"] for s in secs)
    return manifest, patches


def parse_lanes_arg(raw_lanes):
    if not raw_lanes:
        return []
    req = [x.strip() for x in str(raw_lanes).split(",") if x.strip()]
    if not req:
        raise ValueError("--lanes requires at least one lane name")
    invalid = [ln for ln in req if ln not in VALID_LANES]
    if invalid:
        raise ValueError(f"unknown lane(s) in --lanes: {', '.join(invalid)}")
    return list(dict.fromkeys(req))


def _low_effort_plan(lines_count, threshold_label, no_tier, raw_lanes, files_list=None, file_count=None):
    req_lanes = parse_lanes_arg(raw_lanes)
    focus_suffix = f" (focus: {', '.join(req_lanes)})" if req_lanes else ""
    inline = lines_count < 400
    tier = "inline" if inline else ("standard" if no_tier else "fast")
    why = f"{threshold_label} under 400 lines{focus_suffix}" if inline else f"{threshold_label} single lane{focus_suffix}"
    entry = {"lane": "critical-risk", "tier": tier, "why": why}
    if files_list is not None:
        entry["files_list"] = files_list
    if file_count is not None:
        entry["file_count"] = file_count
    if req_lanes:
        entry["focus_lanes"] = req_lanes
    plan = {"selected": [entry], "folded": [], "skipped": [], "inline_adherence": inline}
    if req_lanes:
        plan["focus_lanes"] = req_lanes
    return plan


def _apply_cap_overflow(overflow, fold_target, fold_why, skip_fold_prefix, cap_why):
    folded = []
    skipped = []
    for ln, _ in overflow:
        if fold_target and ln in ("security", "performance", "compatibility", "robustness"):
            folded.append({"lane": ln, "into": fold_target, "why": fold_why})
            skipped.append({"lane": ln, "why": f"folded into {fold_target} ({skip_fold_prefix})"})
        else:
            skipped.append({"lane": ln, "why": cap_why})
    return folded, skipped


def select_lanes(effort, signals, lane_manifest, persona, rules_changed, args, small_pr, core_lines=None):
    """Apply conditional triggers, micro-lane folding, effort caps, overrides, and tiering."""
    if effort == "skim":
        tier = "standard" if args.no_tier else "fast"
        return {
            "selected": [{"lane": "skim", "tier": tier, "why": "skim profile"}],
            "folded": [], "skipped": [], "inline_adherence": False,
        }
    if effort == "low":
        if core_lines is None:
            core_lines = sum(
                s["changed_lines"] for k, s in lane_manifest.items() if k == "code-quality"
            )
        return _low_effort_plan(core_lines, "low effort", args.no_tier, args.lanes)

    # high and medium always run all lanes on standard (inherit) tier
    emphasis = {e.lower() for e in persona.get("emphasis") or []}
    skipped = []
    folded = []

    if args.lanes:
        req = parse_lanes_arg(args.lanes)
        selected = [{"lane": ln, "tier": "standard", "why": "explicit --lanes override"} for ln in req]
        return {"selected": selected, "folded": [], "skipped": [], "inline_adherence": False}

    if args.no_inherit:
        sel = [{"lane": "adherence", "tier": "standard", "why": "always checks PR promises"}]
        conv_trigger = (
            lane_manifest["conventions"]["file_count"] > 0
            or bool(rules_changed)
            or persona.get("has_house_rules")
            or persona.get("has_learned_prefs")
        )
        if conv_trigger:
            sel.append({"lane": "conventions", "tier": "standard", "why": "source or rule surface changed"})
        else:
            skipped.append({"lane": "conventions", "why": "no source, rule, or persona rule surface"})
        return {"selected": sel, "folded": [], "skipped": skipped, "inline_adherence": False}

    candidates = [("adherence", "verifies PR-scoped promises and acceptance criteria")]

    if lane_manifest["code-quality"]["file_count"] > 0:
        candidates.append(("code-quality", "executable source or test files changed"))
    else:
        skipped.append({"lane": "code-quality", "why": "no executable source or test files changed"})

    conv_trigger = (
        lane_manifest["conventions"]["file_count"] > 0
        or bool(rules_changed)
        or persona.get("has_house_rules")
        or persona.get("has_learned_prefs")
    )
    if conv_trigger:
        candidates.append(("conventions", "source files, repo rules, or active persona rules apply"))
    else:
        skipped.append({"lane": "conventions", "why": "no source, rule-file, or persona rule surface"})

    cond_specs = [
        ("security", bool(signals.get("auth") or signals.get("input") or signals.get("deps") or "security" in emphasis), "auth/input/deps signal or emphasis"),
        ("performance", bool(signals.get("perf") or signals.get("data") or "performance" in emphasis), "perf/data signal or emphasis"),
        ("compatibility", bool(signals.get("contract") or signals.get("data") or "compatibility" in emphasis or "backward-compatibility" in emphasis), "contract/data signal or emphasis"),
        ("robustness", bool(signals.get("async") or "robustness" in emphasis), "async signal or emphasis"),
    ]
    # Boost emphasized conditional lanes
    cond_specs.sort(key=lambda item: (0 if (item[0] in emphasis or (item[0] == "compatibility" and "backward-compatibility" in emphasis)) else 1))

    active_names = {c[0] for c in candidates}
    fold_target = "conventions" if "conventions" in active_names else ("code-quality" if "code-quality" in active_names else None)

    for lane_name, triggered, why in cond_specs:
        if not triggered:
            skipped.append({"lane": lane_name, "why": "no matching diff signal"})
            continue
        stats = lane_manifest[lane_name]
        if stats["file_count"] == 0:
            skipped.append({"lane": lane_name, "why": "no eligible files in lane slice"})
            continue
        # Micro-lane auto-folding for performance, compatibility, robustness (never security on auth/input)
        if (
            lane_name in MICRO_FOLDABLE_LANES
            and fold_target is not None
            and stats["file_count"] <= MICRO_FOLD_MAX_FILES
            and stats["changed_lines"] <= MICRO_FOLD_MAX_LINES
        ):
            folded.append({
                "lane": lane_name, "into": fold_target,
                "why": f"micro-lane auto-fold ({stats['changed_lines']} lines across {stats['file_count']} file(s))",
            })
            skipped.append({"lane": lane_name, "why": f"folded into {fold_target} ({stats['changed_lines']} lines)"})
            continue
        candidates.append((lane_name, why))

    cap = 3 if effort == "medium" else 6
    if small_pr:
        # Lead runs adherence inline; launch up to 2 highest-priority remaining lanes
        rem = [c for c in candidates if c[0] != "adherence"]
        kept_rem = rem[:2]
        f_ov, s_ov = _apply_cap_overflow(rem[2:], fold_target, "small-PR cap fold", "small-PR mode", "small-PR cap")
        folded.extend(f_ov)
        skipped.extend(s_ov)
        return {
            "selected": [{"lane": "adherence", "tier": "inline", "why": "small-PR inline adherence"}]
            + [{"lane": ln, "tier": "standard", "why": w} for ln, w in kept_rem],
            "folded": folded, "skipped": skipped, "inline_adherence": True,
        }

    kept = candidates[:cap]
    f_ov, s_ov = _apply_cap_overflow(candidates[cap:], fold_target, f"{effort} effort lane cap ({cap})", f"cap {cap}", f"exceeded cap ({cap})")
    folded.extend(f_ov)
    skipped.extend(s_ov)

    return {
        "selected": [{"lane": ln, "tier": "standard", "why": w} for ln, w in kept],
        "folded": folded, "skipped": skipped, "inline_adherence": False,
    }


def shard_codebase_records(lane, records, max_files, max_loc):
    """Deterministically partition a large codebase lane slice into directory/module shards."""
    total_lines = sum(r["lines"] for r in records)
    if len(records) <= max_files and total_lines <= max_loc:
        return []

    by_dir = {}
    for rec in records:
        top = rec["path"].split("/", 1)[0] if "/" in rec["path"] else "."
        by_dir.setdefault(top, []).append(rec)

    raw_shards = []
    cur_recs, cur_dirs, cur_lines = [], [], 0

    def flush():
        nonlocal cur_recs, cur_dirs, cur_lines
        if cur_recs:
            raw_shards.append((list(cur_dirs), list(cur_recs), cur_lines))
            cur_recs, cur_dirs, cur_lines = [], [], 0

    for top_dir in sorted(by_dir.keys()):
        group = by_dir[top_dir]
        if len(group) > max_files or sum(r["lines"] for r in group) > max_loc:
            flush()
            sub_recs, sub_lines = [], 0
            for rec in group:
                if sub_recs and (len(sub_recs) + 1 > max_files or sub_lines + rec["lines"] > max_loc):
                    raw_shards.append(([top_dir], sub_recs, sub_lines))
                    sub_recs, sub_lines = [], 0
                sub_recs.append(rec)
                sub_lines += rec["lines"]
            if sub_recs:
                raw_shards.append(([top_dir], sub_recs, sub_lines))
            continue

        g_lines = sum(r["lines"] for r in group)
        if cur_recs and (len(cur_recs) + len(group) > max_files or cur_lines + g_lines > max_loc):
            flush()
        cur_recs.extend(group)
        if top_dir not in cur_dirs:
            cur_dirs.append(top_dir)
        cur_lines += g_lines
    flush()

    shards = []
    for idx, (dirs, recs, lines) in enumerate(raw_shards, 1):
        shards.append({
            "shard_id": f"{lane}-shard-{idx}",
            "scope": ", ".join(dirs[:4]) + (" ..." if len(dirs) > 4 else ""),
            "files": [r["path"] for r in recs],
            "file_count": len(recs),
            "total_lines": lines,
            "files_list": f"lanes/{lane}-shard-{idx}.files.txt",
        })
    return shards


def slice_codebase_lanes(records, signals, max_shard_files=CODEBASE_SHARD_MAX_FILES, max_shard_loc=CODEBASE_SHARD_MAX_LOC):
    """Build per-lane ordered file lists (priority signal files first) and optional module shards."""
    sig_sets = {k: set(v) for k, v in signals.items()}
    all_code = [r for r in records if r["is_code"] and not r["is_gen"]]
    non_test_code = [r for r in all_code if not r["is_test"]]
    rules = [r for r in records if r["is_rule"] and not r["is_gen"]]
    specs_docs = [
        r for r in records
        if (r["is_spec"] or r["is_doc"] or os.path.basename(r["path"]) in CODEBASE_DOC_BASENAMES)
        and not r["is_test"] and not r["is_gen"]
    ]
    manifests_ci = [
        r for r in records
        if (
            is_lock_or_manifest(r["path"])
            or os.path.basename(r["path"]) == "Dockerfile"
            or r["path"].startswith(".github/workflows/")
        )
        and not r["is_gen"]
    ]

    def order_priority(pool, priority_paths):
        seen = set()
        out = []
        for r in pool:
            if r["path"] in priority_paths and r["path"] not in seen:
                out.append(r)
                seen.add(r["path"])
        for r in pool:
            if r["path"] not in seen:
                out.append(r)
                seen.add(r["path"])
        return out

    sec_prio = sig_sets.get("auth", set()) | sig_sets.get("input", set()) | sig_sets.get("deps", set())
    perf_prio = sig_sets.get("perf", set()) | sig_sets.get("data", set()) | sig_sets.get("ui", set())
    compat_prio = sig_sets.get("contract", set()) | sig_sets.get("data", set())
    rob_prio = sig_sets.get("async", set()) | sig_sets.get("data", set())
    all_prio = sec_prio | perf_prio | compat_prio | rob_prio
    critical_pool = order_priority(non_test_code + manifests_ci, all_prio) or specs_docs

    by_lane = {
        "adherence": specs_docs if specs_docs else non_test_code,
        "code-quality": all_code,
        "conventions": order_priority(rules + non_test_code, {r["path"] for r in rules}),
        "security": order_priority(non_test_code + manifests_ci, sec_prio),
        "performance": order_priority(non_test_code, perf_prio),
        "compatibility": order_priority(non_test_code + manifests_ci, compat_prio) if (non_test_code or compat_prio) else [],
        "robustness": order_priority(non_test_code, rob_prio),
        "critical-risk": critical_pool,
    }
    prio_by_lane = {
        "adherence": {r["path"] for r in specs_docs},
        "code-quality": set(),
        "conventions": {r["path"] for r in rules},
        "security": sec_prio,
        "performance": perf_prio,
        "compatibility": compat_prio,
        "robustness": rob_prio,
        "critical-risk": all_prio,
    }

    manifest = {}
    file_lists = {}
    for lane, recs in by_lane.items():
        lines = sum(r["lines"] for r in recs)
        prio_set = prio_by_lane[lane]
        prio_files = [r["path"] for r in recs if r["path"] in prio_set]
        shards = [] if lane == "critical-risk" else shard_codebase_records(lane, recs, max_shard_files, max_shard_loc)
        manifest[lane] = {
            "files": [r["path"] for r in recs],
            "priority_files": prio_files,
            "file_count": len(recs),
            "total_lines": lines,
            "changed_lines": lines,
            "files_list": f"lanes/{lane}.files.txt",
            "shards": shards,
        }
        file_lists[f"{lane}.files.txt"] = "\n".join(
            f"{r['path']}\t{r['lines']}L\t{r['lang']}" + ("\tpriority" if r["path"] in prio_set else "")
            for r in recs
        ) + ("\n" if recs else "")
        for sh in shards:
            sh_set = set(sh["files"])
            sh_recs = [r for r in recs if r["path"] in sh_set]
            file_lists[f"{sh['shard_id']}.files.txt"] = "\n".join(
                f"{r['path']}\t{r['lines']}L\t{r['lang']}" + ("\tpriority" if r["path"] in prio_set else "")
                for r in sh_recs
            ) + ("\n" if sh_recs else "")
    return manifest, file_lists


def select_codebase_lanes(effort, lane_manifest, persona, rules_discovered, args):
    """Select lanes for exhaustive CODEBASE review (all 7 lanes at high, no micro-fold, shards expanded)."""
    if effort == "low":
        cr_stats = lane_manifest.get("critical-risk") or {}
        files_list = cr_stats.get("files_list") or "lanes/critical-risk.files.txt"
        file_count = cr_stats.get("file_count", 0)
        code_lines = cr_stats.get("total_lines")
        if code_lines is None:
            code_lines = lane_manifest["code-quality"]["total_lines"]
        return _low_effort_plan(
            code_lines,
            "low effort codebase",
            args.no_tier,
            args.lanes,
            files_list=files_list,
            file_count=file_count,
        )

    def expand(lane_name, why):
        stats = lane_manifest.get(lane_name) or {}
        shards = stats.get("shards") or []
        if shards:
            return [
                {
                    "lane": lane_name, "shard": sh["shard_id"], "scope": sh["scope"],
                    "files_list": sh["files_list"], "file_count": sh["file_count"],
                    "tier": "standard", "why": f"{why} (shard {sh['shard_id']}: {sh['scope']})",
                }
                for sh in shards
            ]
        return [{
            "lane": lane_name, "files_list": stats.get("files_list"),
            "file_count": stats.get("file_count", 0), "tier": "standard", "why": why,
        }]

    if args.lanes:
        req = parse_lanes_arg(args.lanes)
        selected = []
        for ln in req:
            selected.extend(expand(ln, "explicit --lanes override"))
        return {"selected": selected, "folded": [], "skipped": [], "inline_adherence": False}

    skipped = []
    if args.no_inherit:
        selected = expand("adherence", "verifies codebase against repo specs and documented contracts")
        if lane_manifest["conventions"]["file_count"] > 0 or rules_discovered or persona.get("has_house_rules") or persona.get("has_learned_prefs"):
            selected.extend(expand("conventions", "exhaustive codebase conventions and repo rules audit"))
        else:
            skipped.append({"lane": "conventions", "why": "no source, rule, or persona rule surface"})
        return {"selected": selected, "folded": [], "skipped": skipped, "inline_adherence": False}

    emphasis = {e.lower() for e in persona.get("emphasis") or []}
    candidates = [("adherence", "verifies codebase against specs, docs, and architectural contracts")]

    lane_whys = [
        ("code-quality", "exhaustive correctness, design, error-handling, and testability audit"),
        ("conventions", "exhaustive codebase conventions, repo rules, and persona rules audit"),
        ("security", "exhaustive auth, input, secrets, and dependency security audit"),
        ("performance", "exhaustive query, I/O, caching, memory, and hot-path audit"),
        ("compatibility", "exhaustive API/schema contracts, versioning, and migration safety audit"),
        ("robustness", "exhaustive concurrency, transaction, async, and resource-lifecycle audit"),
    ]
    core_lanes = lane_whys[:2]
    spec_lanes = lane_whys[2:]
    spec_lanes.sort(key=lambda item: (0 if (item[0] in emphasis or (item[0] == "compatibility" and "backward-compatibility" in emphasis)) else 1))

    for ln, why in core_lanes + spec_lanes:
        stats = lane_manifest[ln]
        if stats["file_count"] > 0 or (ln == "conventions" and (rules_discovered or persona.get("has_house_rules") or persona.get("has_learned_prefs"))):
            candidates.append((ln, why))
        else:
            skipped.append({"lane": ln, "why": "no eligible files in codebase scope"})

    cap = 3 if effort == "medium" else 7
    kept = candidates[:cap]
    active_names = {c[0] for c in kept}
    fold_target = "conventions" if "conventions" in active_names else ("code-quality" if "code-quality" in active_names else None)
    folded, overflow_skipped = _apply_cap_overflow(
        candidates[cap:], fold_target, f"{effort} effort lane cap ({cap})", f"cap {cap}", f"exceeded cap ({cap})"
    )
    skipped.extend(overflow_skipped)

    selected = []
    for ln, why in kept:
        selected.extend(expand(ln, why))
    return {"selected": selected, "folded": folded, "skipped": skipped, "inline_adherence": False}


# ---------------------------------------------------------------------------
# Intent hints and carry state preparation
# ---------------------------------------------------------------------------

def extract_intent_hints(pr_data, sections, clone, head_sha, cli_specs, owner=None, repo=None):
    title = str(pr_data.get("title") or "")
    body = str(pr_data.get("body") or "")
    branch = str(pr_data.get("headRefName") or "")
    commits = [
        (c.get("messageHeadline") if isinstance(c, dict) else str(c))
        for c in (pr_data.get("commits") or [])
    ]
    blob = "\n".join([title, body, branch] + commits)

    tickets = []
    for key in sorted(set(TRACKER_KEY_RX.findall(blob))):
        if not any(key.startswith(pref) for pref in NON_TICKET_PREFIXES):
            tickets.append(key)

    gh_issues = []
    seen_issues = set()

    def add_issue(val):
        if val and val not in seen_issues:
            seen_issues.add(val)
            gh_issues.append(val)

    for url in sorted(set(GH_ISSUE_URL_RX.findall(blob))):
        add_issue(url)
    for ref in pr_data.get("closingIssuesReferences") or []:
        if isinstance(ref, dict) and ref.get("url"):
            add_issue(ref["url"])
    for o, n in sorted(set(GH_ISSUE_SHORTHAND_RX.findall(blob))):
        if int(n) > 0:
            add_issue(f"https://github.com/{o}/issues/{n}")
    for n in sorted(set(GH_ISSUE_LOCAL_RX.findall(blob)), key=int):
        if int(n) <= 0:
            continue
        if owner and repo and owner != "local":
            add_issue(f"https://github.com/{owner}/{repo}/issues/{n}")
        else:
            add_issue(f"#{n}")

    external_urls = []
    for raw_u in sorted(set(URL_RX.findall(blob))):
        u = raw_u.rstrip(".,;:!?")
        if "/pull/" in u or "/issues/" in u:
            continue
        if u and u not in external_urls:
            external_urls.append(u)

    diff_specs = [s["path"] for s in sections if s["status"] != "deleted" and is_spec_file(s["path"])]
    repo_specs = []
    if clone and head_sha and SAFE_SHA.match(head_sha):
        rc, out, _ = run_cmd(["git", "-C", clone, "ls-tree", "-r", "-z", "--name-only", head_sha])
        if rc == 0:
            for p in out.split("\x00"):
                if p and is_spec_file(p) and p not in diff_specs:
                    repo_specs.append(p)

    body_lines = [ln for ln in body.splitlines() if ln.strip()]
    desc_status = "empty" if not body_lines else ("one-line" if len(body_lines) <= 1 else "ok")

    return {
        "cli_specs": list(cli_specs or []),
        "diff_specs": diff_specs,
        "repo_specs": repo_specs[:20],
        "tickets": tickets,
        "github_issues": gh_issues,
        "external_urls": external_urls[:15],
        "pr_description": desc_status,
    }


def _text_mentions_spec(pr_data):
    blob = " ".join([
        str(pr_data.get("title") or ""),
        str(pr_data.get("body") or ""),
        str(pr_data.get("headRefName") or ""),
    ])
    for tok in re.split(r"\s+", blob):
        cleaned = tok.strip("'\"`()[]{}.,;:!?<>")
        if cleaned and ("/" in cleaned or "." in cleaned) and is_spec_file(cleaned):
            return True
    return False


def find_previous_findings(reviews_dir, owner, repo, number, extra_dirs=None):
    dirs = []
    if reviews_dir:
        dirs.append(reviews_dir)
    if extra_dirs:
        dirs.extend(extra_dirs)
    elif reviews_dir and os.path.abspath(reviews_dir) == os.path.abspath(DEFAULT_REVIEWS_DIR):
        dirs.append(LEGACY_REVIEWS_DIR)

    prefix = f"{owner}-{repo}-{number}-"
    matches = []
    seen_dirs = set()
    for d in dirs:
        if not d:
            continue
        ad = os.path.abspath(d)
        if ad in seen_dirs or not os.path.isdir(ad):
            continue
        seen_dirs.add(ad)
        for name in os.listdir(ad):
            if not name.startswith(prefix):
                continue
            fp = os.path.join(ad, name, "findings.json")
            if os.path.isfile(fp):
                try:
                    with open(fp, encoding="utf-8") as fh:
                        doc = json.load(fh)
                    rank = 2 if (isinstance(doc, dict) and "next_id" in doc) else 1
                    matches.append((rank, os.path.getmtime(fp), fp))
                except ValueError:
                    matches.append((0, os.path.getmtime(fp), fp))
                except OSError:
                    continue
    if not matches:
        return None
    matches.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return matches[0][2]


def _path_in_scope(path, scope_path):
    if not scope_path or scope_path == ".":
        return True
    return path == scope_path or path.startswith(scope_path + "/")


def prepare_carry_state(prev_path, bundle_dir, coverage_limits, scope_path=None):
    """Load previous findings.json safely, write bundle carry files, and return (carry_info, state)."""
    carry_info = {"enabled": False, "delta_mode": False, "previous_path": None, "history_rewritten": False}
    if scope_path is not None:
        carry_info["scope_path"] = scope_path or "."
    if not prev_path:
        return carry_info, None

    prev_scope = None
    try:
        with open(prev_path, encoding="utf-8") as fh:
            raw_prev = json.load(fh)
        if isinstance(raw_prev, dict):
            prev_scope = raw_prev.get("scope_path") or (
                raw_prev.get("codebase", {}).get("scope_path") if isinstance(raw_prev.get("codebase"), dict) else None
            )
        state, _id_map, warnings = carry_findings.load_previous(prev_path)
    except (carry_findings.BadInput, OSError, ValueError) as exc:
        msg = f"carry skipped: {exc}"
        coverage_limits.append(msg)
        carry_info["warnings"] = [msg]
        return carry_info, None

    if scope_path and scope_path != ".":
        kept_findings = []
        filtered_out = 0
        for f in state["findings"]:
            fpath, _ = carry_findings.file_and_lines(f)
            if fpath and _path_in_scope(fpath, scope_path):
                kept_findings.append(f)
            else:
                filtered_out += 1
        if filtered_out or (prev_scope and prev_scope != scope_path):
            warnings.append(
                f"codebase carry scoped to '{scope_path}' (previous scope: {prev_scope or 'unknown'}; "
                f"filtered {filtered_out} out-of-scope finding(s))"
            )
        state["findings"] = kept_findings

    declines = [carry_findings.decline_row(f) for f in state["findings"] if f["status"] == "declined"]
    open_ = [
        carry_findings.open_row(f)
        for f in state["findings"]
        if f["status"] == "todo" and f["severity"] in carry_findings.BLOCKING
    ]
    carry_findings.write_json(os.path.join(bundle_dir, "previous.json"), state)
    carry_findings.write_json(os.path.join(bundle_dir, "declines.json"), declines)
    carry_findings.write_json(os.path.join(bundle_dir, "open.json"), open_)
    carry_info.update({
        "enabled": True,
        "previous_path": prev_path,
        "last_sha": state.get("last_sha"),
        "declines_count": len(declines),
        "open_count": len(open_),
        "warnings": warnings,
    })
    return carry_info, state


def _apply_delta_diff(clone, target_mode, owner, repo, head_sha, carry_state, carry_info, full_pr_diff, bundle_dir, coverage_limits, since_last):
    """Attempt incremental delta diff from previous last_sha; return updated diff_text."""
    last_sha = carry_state.get("last_sha")
    if not (last_sha and SAFE_SHA.match(last_sha)):
        return full_pr_diff
    if last_sha == head_sha:
        if since_last:
            carry_info["warnings"].append("already reviewed at head_sha; running full PR diff")
        return full_pr_diff
    if not clone:
        coverage_limits.append("no clone — delta diff skipped, full PR diff reviewed")
        return full_pr_diff

    rc_ce, _, _ = run_cmd(["git", "-C", clone, "cat-file", "-e", f"{last_sha}^{{commit}}"])
    if rc_ce != 0 and target_mode == "pr":
        run_cmd(["git", "-C", clone, "fetch", "-q", "origin", last_sha])
        rc_ce, _, _ = run_cmd(["git", "-C", clone, "cat-file", "-e", f"{last_sha}^{{commit}}"])
    if rc_ce == 0:
        rc_anc, _, _ = run_cmd(["git", "-C", clone, "merge-base", "--is-ancestor", last_sha, head_sha])
        if rc_anc == 0:
            rc_dd, out_dd, _ = run_cmd(["git", "-C", clone, "diff", "-M", f"{last_sha}..{head_sha}"])
            if rc_dd == 0:
                with open(os.path.join(bundle_dir, "pr.diff.patch"), "w", encoding="utf-8") as fh:
                    fh.write(full_pr_diff)
                carry_info["delta_mode"] = True
                return out_dd
        elif rc_anc == 1:
            carry_info["history_rewritten"] = True
        else:
            coverage_limits.append("delta check failed — full PR diff reviewed")
    elif target_mode == "pr":
        rc_cmp, out_cmp, _ = run_cmd([
            "gh", "api", f"repos/{owner}/{repo}/compare/{last_sha}...{head_sha}", "--jq", ".status",
        ])
        if rc_cmp == 0 and out_cmp.strip() == "diverged":
            carry_info["history_rewritten"] = True
        else:
            coverage_limits.append("previous commit unavailable locally — full PR diff reviewed")
    else:
        coverage_limits.append("previous commit unavailable locally — full branch diff reviewed")
    return full_pr_diff


def _copy_changed_rules(sections, bundle_dir, effort, clone, owner, repo, base_sha, coverage_limits):
    """Write rules-changed.txt and copy up to 30 base rule files at high/medium."""
    changed_rules = [s for s in sections if is_rule_file(s["path"])]
    if not changed_rules:
        return []
    base_root = os.path.join(bundle_dir, "base")
    rule_records = []
    copied_count = 0
    for rs in changed_rules:
        p = rs["path"]
        st = rs["status"]
        src_p = rs["old_path"] if st == "renamed" else p
        if st == "added":
            bstate = "none"
        elif effort in ("low", "skim"):
            bstate = "skipped"
        elif (
            copied_count >= 30
            or base_sha == "unavailable"
            or not SAFE_SHA.match(base_sha)
            or not is_safe_relpath(p)
            or not is_safe_relpath(src_p)
        ):
            bstate = "unavailable"
        else:
            copied_count += 1
            if clone:
                rc_b, content_b, _ = run_cmd(["git", "-C", clone, "show", f"{base_sha}:{src_p}"])
            else:
                qsrc = urllib.parse.quote(src_p, safe="/")
                rc_b, content_b, _ = run_cmd([
                    "gh", "api", f"repos/{owner}/{repo}/contents/{qsrc}?ref={base_sha}",
                    "-H", "Accept: application/vnd.github.raw",
                ])
            if rc_b == 0:
                dst = os.path.join(base_root, p)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "w", encoding="utf-8", errors="replace") as fh:
                    fh.write(content_b)
                bstate = "ok"
            else:
                bstate = "unavailable"
        rule_records.append({"status": st, "path": p, "base_state": bstate})
    with open(os.path.join(bundle_dir, "rules-changed.txt"), "w", encoding="utf-8") as fh:
        for r in rule_records:
            fh.write(f"{r['status']}\t{r['path']}\t{r['base_state']}\n")
    unavail = [r["path"] for r in rule_records if r["base_state"] == "unavailable"]
    if unavail:
        coverage_limits.append(f"base rules unavailable for {', '.join(unavail)}; head versions used")
    return rule_records


# ---------------------------------------------------------------------------
# Codebase and PR bundle builders
# ---------------------------------------------------------------------------

def build_codebase(args, cwd):
    """Build exhaustive CODEBASE review bundle (file-map.json, signals, per-lane file lists & shards)."""
    if args.effort == "skim":
        raise ValueError("--effort=skim is not supported with level=CODEBASE; use high, medium, or low")

    scope_path = None
    if args.target is not None and str(args.target).strip():
        t = str(args.target).strip()
        if UNSAFE_SHELL.search(t):
            raise ValueError("unsafe characters in target argument")
        if PR_URL_RX.match(t) or PR_SHORTHAND_RX.match(t) or PR_NUM_RX.match(t):
            raise ValueError("PR target is not allowed with level=CODEBASE; pass a repo-relative path or omit target")
        cleaned = t.strip("/")
        if cleaned.startswith("./"):
            cleaned = cleaned[2:]
        if cleaned and cleaned != ".":
            if not is_safe_relpath(cleaned):
                raise ValueError(f"invalid codebase scope path: {t}")
            scope_path = cleaned

    clone = find_clone(cwd)
    coverage_limits = []
    if clone:
        rc_h, out_h, err_h = run_cmd(["git", "-C", clone, "rev-parse", "HEAD"])
        if rc_h != 0 or not SAFE_SHA.match(out_h.strip()):
            raise RuntimeError(f"cannot resolve HEAD: {err_h.strip()}")
        head_sha = out_h.strip()
        sha7 = head_sha[:7]
        rc_br, out_br, _ = run_cmd(["git", "-C", clone, "rev-parse", "--abbrev-ref", "HEAD"])
        head_ref = out_br.strip() if rc_br == 0 else "HEAD"
        rc_u, url, _ = run_cmd(["git", "-C", clone, "remote", "get-url", "origin"])
        ro, rr = _parse_github_remote(url) if rc_u == 0 else (None, None)
        owner = ro or "local"
        repo = rr or os.path.basename(os.path.abspath(clone))

        rc_ls, out_ls, err_ls = run_cmd(["git", "-C", clone, "ls-tree", "-r", "-z", head_sha])
        if rc_ls != 0:
            raise RuntimeError(f"git ls-tree failed: {err_ls.strip()}")
        raw_paths = []
        for rec in out_ls.split("\x00"):
            if not rec or "\t" not in rec:
                continue
            meta, p = rec.split("\t", 1)
            parts = meta.split()
            mode = parts[0] if parts else ""
            if mode in ("120000", "160000"):
                continue
            if p:
                raw_paths.append(p)
    else:
        head_sha = "working-tree"
        sha7 = "worktree"
        head_ref = "working-tree"
        owner = "local"
        repo = os.path.basename(cwd)
        coverage_limits.append("not a git repository — working tree scanned directly")
        raw_paths = []
        real_cwd = os.path.realpath(cwd)
        for root, dirs, files in os.walk(cwd):
            dirs[:] = sorted(
                d for d in dirs
                if d not in WALK_SKIP_DIRS and not os.path.islink(os.path.join(root, d))
            )
            for fn in sorted(files):
                full = os.path.join(root, fn)
                if os.path.islink(full):
                    continue
                real_f = os.path.realpath(full)
                if not (real_f == real_cwd or real_f.startswith(real_cwd + os.sep)):
                    continue
                rel = os.path.relpath(full, cwd).replace(os.sep, "/")
                raw_paths.append(rel)

    if scope_path:
        raw_paths = [p for p in raw_paths if _path_in_scope(p, scope_path)]
        if not raw_paths:
            raise ValueError(f"codebase scope path matched no files: {scope_path}")

    candidate_paths = []
    for p in sorted(set(raw_paths)):
        if not is_safe_relpath(p) or is_denylisted(p) or is_generated_or_locale(p):
            continue
        candidate_paths.append(p)

    real_cwd = os.path.realpath(cwd)

    def inspect_one(path):
        if clone:
            rc, content, _ = run_cmd(["git", "-C", clone, "show", f"{head_sha}:{path}"])
            if rc != 0:
                return None
        else:
            full = os.path.join(cwd, path)
            if os.path.islink(full):
                return None
            real_f = os.path.realpath(full)
            if not (real_f == real_cwd or real_f.startswith(real_cwd + os.sep)):
                return None
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                return None
        if "\x00" in content[:8192]:
            return None
        lines = len(content.splitlines())
        is_doc = is_doc_or_asset(path)
        if is_doc and not is_lock_or_manifest(path):
            matched_sigs = []
        else:
            haystack = path + "\n" + content[:CODEBASE_SCAN_MAX_BYTES]
            matched_sigs = [sig for sig, rx in SIGNAL_RULES.items() if rx.search(haystack)]
        return {
            "path": path,
            "lines": lines,
            "lang": lang_of(path),
            "is_code": is_executable_code(path),
            "is_test": is_test_path(path),
            "is_rule": is_rule_file(path),
            "is_spec": is_spec_file(path),
            "is_doc": is_doc,
            "is_gen": is_generated_or_locale(path),
            "signals": matched_sigs,
        }

    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(inspect_one, candidate_paths):
            if res is not None:
                records.append(res)

    bundle_dir = _resolve_bundle_dir(args.bundle_dir, owner, repo, "codebase", sha7)

    prev_path = None if args.fresh else find_previous_findings(args.reviews_dir, owner, repo, "codebase")
    carry_info, _ = prepare_carry_state(prev_path, bundle_dir, coverage_limits, scope_path=scope_path or ".")

    file_map = {r["path"]: {"lines": r["lines"], "lang": r["lang"]} for r in records}
    with open(os.path.join(bundle_dir, "file-map.json"), "w", encoding="utf-8") as fh:
        json.dump(file_map, fh, indent=2)
    with open(os.path.join(bundle_dir, "files.txt"), "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(f"{r['path']}\tcodebase\t{r['lines']}L\t{r['lang']}\n")
    with open(os.path.join(bundle_dir, "existing-comments.json"), "w", encoding="utf-8") as fh:
        json.dump({"inline": [], "reviews": []}, fh, indent=2)

    signals = {}
    for r in records:
        for sig in r["signals"]:
            signals.setdefault(sig, []).append(r["path"])
    with open(os.path.join(bundle_dir, "signals.txt"), "w", encoding="utf-8") as fh:
        for sig, paths in sorted(signals.items()):
            fh.write(f"{sig}: {', '.join(paths)}\n")

    rules_discovered = [r["path"] for r in records if r["is_rule"]]
    if rules_discovered:
        with open(os.path.join(bundle_dir, "rules-discovered.txt"), "w", encoding="utf-8") as fh:
            for p in rules_discovered:
                fh.write(p + "\n")

    repo_specs = [
        r["path"] for r in records
        if r["is_spec"] or os.path.basename(r["path"]) in CODEBASE_DOC_BASENAMES
    ]
    intent_hints = {
        "cli_specs": list(args.spec or []),
        "diff_specs": [],
        "repo_specs": repo_specs[:40],
        "tickets": [],
        "github_issues": [],
        "external_urls": [],
        "pr_description": "codebase",
    }

    effort = "high" if args.effort == "auto" else args.effort
    effort_reason = "exhaustive codebase review (auto -> high)" if args.effort == "auto" else "explicit flag"

    max_sf = args.shard_max_files
    max_sl = args.shard_max_loc
    lane_manifest, lane_file_lists = slice_codebase_lanes(records, signals, max_sf, max_sl)
    lanes_dir = os.path.join(bundle_dir, "lanes")
    os.makedirs(lanes_dir, exist_ok=True)
    for fname, text in lane_file_lists.items():
        with open(os.path.join(lanes_dir, fname), "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(os.path.join(lanes_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(lane_manifest, fh, indent=2)

    persona = parse_persona_frontmatter(args.persona)
    lane_plan = select_codebase_lanes(effort, lane_manifest, persona, rules_discovered, args)

    total_lines = sum(r["lines"] for r in records)
    code_lines = sum(r["lines"] for r in records if r["is_code"] and not r["is_test"])

    summary = {
        "level": "CODEBASE",
        "gate_g0": "pass",
        "target_mode": "codebase",
        "bundle_dir": bundle_dir,
        "clone": clone,
        "pr": {
            "owner": owner, "repo": repo, "number": 0,
            "url": "", "title": f"Codebase review ({repo}" + (f"/{scope_path}" if scope_path else "") + ")",
            "head_sha": head_sha, "sha7": sha7,
            "base_sha": "n/a", "base_ref": head_ref,
            "author_is_me": True, "is_draft": False,
            "ci": "none",
            "additions": total_lines, "deletions": 0, "changed_files": len(records),
            "core_changed_lines": code_lines, "skim_changed_lines": total_lines,
        },
        "codebase": {
            "scope_path": scope_path or ".",
            "total_files": len(records),
            "code_files": sum(1 for r in records if r["is_code"]),
            "total_lines": total_lines,
            "code_lines": code_lines,
            "rules_discovered": rules_discovered,
            "sharded_lanes": [k for k, v in lane_manifest.items() if v.get("shards")],
        },
        "gate_g0b": {
            "draft": False, "bot": False,
            "lockfile_only": False, "in_skim_profile": False,
        },
        "effort": {"resolved": effort, "reason": effort_reason, "small_pr": False},
        "persona": _persona_summary(persona),
        "signals": signals,
        "lane_plan": lane_plan,
        "lane_slices": {
            k: {
                "files": v["file_count"], "lines": v["total_lines"],
                "files_list": v["files_list"], "shard_count": len(v.get("shards") or []),
            }
            for k, v in lane_manifest.items()
        },
        "intent_hints": intent_hints,
        "carry": carry_info,
        "rules_changed": [],
        "rules_discovered": rules_discovered,
        "existing_comments_count": 0,
        "context_files_count": 0,
        "scope_truncated": False,
        "coverage_limits": coverage_limits,
    }
    with open(os.path.join(bundle_dir, "bundle-summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    return summary


def build(args):
    cwd = os.path.abspath(args.cwd)
    if not os.path.isdir(cwd):
        raise ValueError(f"--cwd directory does not exist: {cwd}")
    if args.shard_max_files < 1 or args.shard_max_loc < 1:
        raise ValueError("--shard-max-files and --shard-max-loc must be >= 1")
    parse_lanes_arg(args.lanes)

    level = str(getattr(args, "level", None) or "PR").upper()
    if level == "CODEBASE":
        return build_codebase(args, cwd)
    coverage_limits = []
    mode, owner, repo, number, pr_url = parse_target(args.target, cwd)

    clone = find_clone(cwd, owner, repo)
    pr_data = None
    author_is_me = True
    target_mode = "local_branch"
    inline_comments, review_comments = [], []
    diff_text = ""

    # Auto-detect open PR when no target given and not --local-only
    if mode == "auto" and not args.local_only:
        rc, out, _ = run_cmd(["gh", "pr", "view", "--json", "url,number,state"], cwd=cwd)
        if rc == 0 and out.strip():
            try:
                view = json.loads(out)
                if isinstance(view, dict) and str(view.get("state") or "").upper() == "OPEN" and view.get("url"):
                    mode, owner, repo, number, pr_url = parse_target(view["url"], cwd)
                    clone = find_clone(cwd, owner, repo)
            except ValueError:
                pass

    if mode == "pr" and not args.local_only:
        target_mode = "pr"
        pr_fields = (
            "number,title,body,author,state,isDraft,baseRefName,baseRefOid,"
            "headRefName,headRefOid,additions,deletions,changedFiles,commits,"
            "labels,closingIssuesReferences,statusCheckRollup,url"
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f_pr = ex.submit(run_cmd, ["gh", "pr", "view", pr_url, "--json", pr_fields])
            f_me = ex.submit(run_cmd, ["gh", "api", "user", "--jq", ".login"])
            rc_pr, out_pr, err_pr = f_pr.result()
            rc_me, out_me, _ = f_me.result()
        if rc_pr != 0:
            raise RuntimeError(f"gh pr view failed: {err_pr.strip()}")
        try:
            pr_data = json.loads(out_pr)
        except ValueError as exc:
            raise RuntimeError(f"invalid JSON from gh pr view: {exc}") from exc
        if not isinstance(pr_data, dict):
            raise RuntimeError("unexpected non-object JSON from gh pr view")

        me_login = out_me.strip() if rc_me == 0 else ""
        pr_author = (pr_data.get("author") or {}).get("login") or ""
        author_is_me = bool(me_login and pr_author and me_login == pr_author)
        # Gate G0
        if str(pr_data.get("state") or "").upper() != "OPEN":
            return {
                "gate_g0": "closed_or_merged",
                "state": pr_data.get("state"),
                "pr": {"owner": owner, "repo": repo, "number": int(number), "url": pr_url},
            }

        head_sha = str(pr_data.get("headRefOid") or "")
        if not SAFE_SHA.match(head_sha):
            raise RuntimeError("invalid headRefOid SHA from gh pr view")
        base_ref_oid = str(pr_data.get("baseRefOid") or "")

        # Parallel network + git fetches
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            f_diff = ex.submit(run_cmd, ["gh", "pr", "diff", pr_url])
            f_ic = ex.submit(
                run_cmd,
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{number}/comments", "--paginate",
                 "--jq", "[.[] | {path, line, body, resolved: false}]"],
            )
            f_rc = ex.submit(
                run_cmd,
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{number}/reviews", "--paginate",
                 "--jq", "[.[] | {state, body}]"],
            )
            f_fetch_head = ex.submit(run_cmd, ["git", "-C", clone, "fetch", "-q", "origin", f"refs/pull/{number}/head"]) if clone else None
            f_fetch_base = ex.submit(run_cmd, ["git", "-C", clone, "fetch", "-q", "origin", base_ref_oid]) if (clone and SAFE_SHA.match(base_ref_oid)) else None

            rc_d, out_d, err_d = f_diff.result()
            if rc_d != 0:
                raise RuntimeError(f"gh pr diff failed: {err_d.strip()}")
            diff_text = out_d

            rc_ic, out_ic, _ = f_ic.result()
            if rc_ic == 0:
                inline_comments, ic_ok = parse_paginated_json_list(out_ic)
                if not ic_ok:
                    coverage_limits.append("existing inline comments unparseable")
            else:
                coverage_limits.append("existing inline comments unavailable")

            rc_rv, out_rv, _ = f_rc.result()
            if rc_rv == 0:
                review_comments, rv_ok = parse_paginated_json_list(out_rv)
                if not rv_ok:
                    coverage_limits.append("existing PR reviews unparseable")
            else:
                coverage_limits.append("existing PR reviews unavailable")

            if f_fetch_head:
                f_fetch_head.result()
                rc_ce, _, _ = run_cmd(["git", "-C", clone, "cat-file", "-e", f"{head_sha}^{{commit}}"])
                if rc_ce != 0:
                    coverage_limits.append("head commit not in local clone — GitHub API fallback used")
                    clone = None
            if f_fetch_base:
                f_fetch_base.result()

        # Resolve merge base
        base_sha = "unavailable"
        if clone and SAFE_SHA.match(base_ref_oid):
            rc_mb, out_mb, _ = run_cmd(["git", "-C", clone, "merge-base", base_ref_oid, head_sha])
            if rc_mb == 0 and SAFE_SHA.match(out_mb.strip()):
                base_sha = out_mb.strip()
            else:
                base_sha = base_ref_oid
                coverage_limits.append("base is the branch tip, not the merge base")
        elif SAFE_SHA.match(base_ref_oid):
            rc_mb, out_mb, _ = run_cmd([
                "gh", "api", f"repos/{owner}/{repo}/compare/{base_ref_oid}...{head_sha}",
                "--jq", ".merge_base_commit.sha",
            ])
            if rc_mb == 0 and SAFE_SHA.match(out_mb.strip()):
                base_sha = out_mb.strip()
            else:
                coverage_limits.append("base commit not available")
        if not clone:
            coverage_limits.append("no clone — GitHub API only")
    else:
        # Local branch diff mode
        if not clone:
            raise RuntimeError("not inside a git repository and no PR target provided")
        rc_h, out_h, err_h = run_cmd(["git", "-C", clone, "rev-parse", "HEAD"])
        if rc_h != 0 or not SAFE_SHA.match(out_h.strip()):
            raise RuntimeError(f"cannot resolve HEAD: {err_h.strip()}")
        head_sha = out_h.strip()
        rc_br, out_br, _ = run_cmd(["git", "-C", clone, "rev-parse", "--abbrev-ref", "HEAD"])
        head_ref = out_br.strip() if rc_br == 0 else "HEAD"

        base_ref = None
        base_sha = "unavailable"
        for cand in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
            if cand == head_ref:
                continue
            rc_c, _, _ = run_cmd(["git", "-C", clone, "rev-parse", "--verify", cand])
            if rc_c == 0:
                rc_mb, out_mb, _ = run_cmd(["git", "-C", clone, "merge-base", cand, head_sha])
                if rc_mb == 0 and SAFE_SHA.match(out_mb.strip()) and out_mb.strip() != head_sha:
                    base_ref = cand
                    base_sha = out_mb.strip()
                    break
        if base_sha == "unavailable":
            rc_p, out_p, _ = run_cmd(["git", "-C", clone, "rev-parse", "--verify", "HEAD~1"])
            if rc_p == 0 and SAFE_SHA.match(out_p.strip()):
                base_ref = "HEAD~1"
                base_sha = out_p.strip()
            else:
                raise RuntimeError("cannot determine base commit for local branch diff")

        rc_d, out_d, err_d = run_cmd(["git", "-C", clone, "diff", "-M", f"{base_sha}..{head_sha}"])
        if rc_d != 0:
            raise RuntimeError(f"git diff failed: {err_d.strip()}")
        diff_text = out_d
        rc_log, out_log, _ = run_cmd(["git", "-C", clone, "log", "--format=%s", f"{base_sha}..{head_sha}"])
        commit_headlines = [ln for ln in out_log.splitlines() if ln.strip()] if rc_log == 0 else []

        owner = "local"
        repo = os.path.basename(os.path.abspath(clone))
        number = "0"
        pr_url = ""
        pr_data = {
            "number": 0, "title": commit_headlines[0] if commit_headlines else f"Local branch {head_ref}",
            "body": "\n".join(commit_headlines), "state": "OPEN", "isDraft": False,
            "author": {"login": "local"}, "baseRefName": base_ref, "baseRefOid": base_sha,
            "headRefName": head_ref, "headRefOid": head_sha,
            "commits": [{"messageHeadline": h} for h in commit_headlines],
            "statusCheckRollup": [], "closingIssuesReferences": [],
        }

    sha7 = head_sha[:7]
    bundle_dir = _resolve_bundle_dir(args.bundle_dir, owner, repo, number, sha7)

    # Carry & delta check
    prev_path = None if args.fresh else find_previous_findings(args.reviews_dir, owner, repo, number)
    full_pr_diff = diff_text
    carry_info, carry_state = prepare_carry_state(prev_path, bundle_dir, coverage_limits)
    if carry_state is not None:
        diff_text = _apply_delta_diff(
            clone, target_mode, owner, repo, head_sha,
            carry_state, carry_info, full_pr_diff, bundle_dir, coverage_limits, args.since_last,
        )

    # Write pr.json, diff.patch, existing-comments.json, diff-map.json
    clean_pr_data = sanitize_pr_data(pr_data)
    with open(os.path.join(bundle_dir, "pr.json"), "w", encoding="utf-8") as fh:
        json.dump(clean_pr_data, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(bundle_dir, "diff.patch"), "w", encoding="utf-8") as fh:
        fh.write(diff_text)
    with open(os.path.join(bundle_dir, "existing-comments.json"), "w", encoding="utf-8") as fh:
        json.dump({"inline": inline_comments, "reviews": review_comments}, fh, indent=2, ensure_ascii=False)

    dmap = diff_map.build(diff_text)
    with open(os.path.join(bundle_dir, "diff-map.json"), "w", encoding="utf-8") as fh:
        json.dump(dmap, fh, indent=2)

    sections = parse_diff_by_file(diff_text)
    with open(os.path.join(bundle_dir, "files.txt"), "w", encoding="utf-8") as fh:
        for s in sections:
            fh.write(f"{s['path']}\t{s['status']}\t+{s['adds']}/-{s['dels']}\t{s['lang']}\n")

    signals = detect_signals(sections)
    with open(os.path.join(bundle_dir, "signals.txt"), "w", encoding="utf-8") as fh:
        for sig, paths in sorted(signals.items()):
            fh.write(f"{sig}: {', '.join(paths)}\n")

    intent_hints = extract_intent_hints(clean_pr_data, sections, clone, head_sha, args.spec, owner=owner, repo=repo)
    total_adds = sum(s["adds"] for s in sections)
    total_dels = sum(s["dels"] for s in sections)
    core_lines = sum(
        s["adds"] + s["dels"] for s in sections
        if not is_test_path(s["path"])
        and not is_generated_or_locale(s["path"])
        and not is_lockfile(s["path"])
        and not is_secret_or_vendor(s["path"])
    )
    skim_lines = sum(
        s["adds"] + s["dels"] for s in sections
        if not is_generated_or_locale(s["path"]) and not is_lockfile(s["path"])
    )
    lock_lines = sum(s["adds"] + s["dels"] for s in sections if is_lockfile(s["path"]))
    lockfile_only = bool(sections) and all(is_lock_or_manifest(s["path"]) for s in sections)

    author_obj = clean_pr_data.get("author") or {}
    is_bot = is_bot_author(author_obj)

    spec_count = len(intent_hints["cli_specs"]) + len(intent_hints["diff_specs"])
    text_names_spec = _text_mentions_spec(clean_pr_data)
    in_skim_profile = (
        skim_lines < 100
        and len(sections) <= 4
        and spec_count == 0
        and not text_names_spec
        and not any(signals.get(k) for k in ("auth", "data", "contract"))
    )

    # Resolve effort
    if args.effort != "auto":
        effort = args.effort
        effort_reason = "explicit flag"
    elif is_bot or lockfile_only or in_skim_profile:
        effort = "skim"
        effort_reason = "bot author" if is_bot else ("lockfile/manifest only" if lockfile_only else f"skim size profile ({skim_lines} lines)")
    elif core_lines < 400 and spec_count == 0 and not any(signals.get(k) for k in ("auth", "data", "contract")):
        effort = "low"
        effort_reason = f"{core_lines} core lines, no spec, no auth/data/contract signal"
    elif core_lines < 1500 and spec_count <= 2:
        effort = "medium"
        effort_reason = f"{core_lines} core lines, {spec_count} spec(s)"
    else:
        effort = "high"
        effort_reason = f"{core_lines} core lines, {spec_count} spec(s)"

    # Rule files and base/ copies
    rule_records = _copy_changed_rules(sections, bundle_dir, effort, clone, owner, repo, base_sha, coverage_limits)

    # Reduced diffs for low / skim
    if effort == "low":
        core_patch = "".join(
            s["text"] for s in sections
            if not is_test_path(s["path"])
            and not is_generated_or_locale(s["path"])
            and not is_lockfile(s["path"])
            and not is_secret_or_vendor(s["path"])
        )
        with open(os.path.join(bundle_dir, "diff.core.patch"), "w", encoding="utf-8") as fh:
            fh.write(core_patch)
    elif effort == "skim":
        skim_patch = "".join(
            s["text"] for s in sections
            if not is_generated_or_locale(s["path"]) and not is_lockfile(s["path"]) and not is_secret_or_vendor(s["path"])
        )
        lock_patch = "".join(s["text"] for s in sections if is_lockfile(s["path"]))
        with open(os.path.join(bundle_dir, "diff.skim.patch"), "w", encoding="utf-8") as fh:
            fh.write(skim_patch)
        with open(os.path.join(bundle_dir, "lock.patch"), "w", encoding="utf-8") as fh:
            fh.write(lock_patch)

    # Full head/ copies & context_files.txt at high and medium
    scope_truncated = False
    context_paths = []
    if effort in ("high", "medium"):
        non_deleted = [s for s in sections if s["status"] != "deleted" and not s["binary"]]
        if len(sections) > 60 or (total_adds + total_dels) > 8000:
            scope_truncated = True
            coverage_limits.append("scope_truncated: >60 files or >8k LOC")
            copy_targets = [
                s["path"] for s in non_deleted
                if not is_test_path(s["path"]) and not is_generated_or_locale(s["path"])
            ]
        else:
            copy_targets = [s["path"] for s in non_deleted]
        head_root = os.path.join(bundle_dir, "head")
        os.makedirs(head_root, exist_ok=True)
        _, failed_copies = copy_snapshot_files(clone, head_sha, copy_targets, head_root, owner, repo)
        real_failed = [p for p in failed_copies if is_safe_relpath(p) and not is_denylisted(p)]
        if real_failed:
            coverage_limits.append(f"head snapshot unavailable for {len(real_failed)} file(s)")

        max_ctx_files = 20 if effort == "high" else 10
        max_ctx_loc = 3000 if effort == "high" else 1500
        ctx_items = find_context_files(clone, head_sha, copy_targets, max_ctx_files, max_ctx_loc)
        for cp, content in ctx_items:
            dst = os.path.join(head_root, cp)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(content)
            context_paths.append(cp)
        with open(os.path.join(bundle_dir, "context_files.txt"), "w", encoding="utf-8") as fh:
            for cp in context_paths:
                fh.write(cp + "\n")

    # Per-lane diff slicing & selection
    lane_manifest, lane_patches = slice_lanes(sections, signals)
    lanes_dir = os.path.join(bundle_dir, "lanes")
    os.makedirs(lanes_dir, exist_ok=True)
    for ln, patch_text in lane_patches.items():
        with open(os.path.join(lanes_dir, f"{ln}.patch"), "w", encoding="utf-8") as fh:
            fh.write(patch_text)
    with open(os.path.join(lanes_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(lane_manifest, fh, indent=2)

    persona = parse_persona_frontmatter(args.persona)
    small_pr = bool(
        args.inline
        or (
            effort in ("high", "medium")
            and (total_adds + total_dels) < 150
            and spec_count <= 1
            and not any(signals.get(k) for k in ("auth", "data", "contract"))
        )
    )
    lane_plan = select_lanes(effort, signals, lane_manifest, persona, rule_records, args, small_pr, core_lines=core_lines)

    summary = {
        "level": "PR",
        "gate_g0": "pass",
        "target_mode": target_mode,
        "bundle_dir": bundle_dir,
        "clone": clone,
        "pr": {
            "owner": owner, "repo": repo, "number": int(number) if str(number).isdigit() else 0,
            "url": pr_url, "title": clean_pr_data.get("title"), "head_sha": head_sha, "sha7": sha7,
            "base_sha": base_sha, "base_ref": clean_pr_data.get("baseRefName"),
            "author_is_me": author_is_me, "is_draft": bool(clean_pr_data.get("isDraft")),
            "ci": compute_ci(clean_pr_data.get("statusCheckRollup")),
            "additions": total_adds, "deletions": total_dels, "changed_files": len(sections),
            "core_changed_lines": core_lines, "skim_changed_lines": skim_lines,
            "lock_changed_lines": lock_lines,
        },
        "gate_g0b": {
            "draft": bool(clean_pr_data.get("isDraft")), "bot": is_bot,
            "lockfile_only": lockfile_only, "in_skim_profile": in_skim_profile,
        },
        "effort": {"resolved": effort, "reason": effort_reason, "small_pr": small_pr},
        "persona": _persona_summary(persona),
        "signals": signals,
        "lane_plan": lane_plan,
        "lane_slices": {k: {"files": v["file_count"], "lines": v["changed_lines"], "patch": v["patch"]} for k, v in lane_manifest.items()},
        "intent_hints": intent_hints,
        "carry": carry_info,
        "rules_changed": rule_records,
        "existing_comments_count": len(inline_comments) + len(review_comments),
        "context_files_count": len(context_paths),
        "scope_truncated": scope_truncated,
        "coverage_limits": coverage_limits,
    }
    with open(os.path.join(bundle_dir, "bundle-summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    return summary


def normalize_argv(argv):
    """Accept bare `level=PR|CODEBASE` kwarg tokens as `--level=...` alongside standard CLI flags."""
    raw = list(sys.argv[1:] if argv is None else argv)
    out = []
    for tok in raw:
        m = re.match(r"^level=(.+)$", tok, re.I)
        if m:
            out.append(f"--level={m.group(1)}")
        else:
            out.append(tok)
    return out


def main(argv=None):
    default_persona = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "reference", "persona.md"))
    ap = argparse.ArgumentParser(prog="build_bundle.py", description=__doc__.splitlines()[0])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--level", type=str.upper, choices=REVIEW_LEVELS, default="PR")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--bundle-dir")
    ap.add_argument("--effort", choices=("auto", "high", "medium", "low", "skim"), default="auto")
    ap.add_argument("--since-last", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--lanes")
    ap.add_argument("--no-inherit", action="store_true")
    ap.add_argument("--inline", action="store_true")
    ap.add_argument("--no-tier", action="store_true")
    ap.add_argument("--spec", action="append", default=[])
    ap.add_argument("--persona", default=default_persona)
    ap.add_argument("--reviews-dir", default=DEFAULT_REVIEWS_DIR)
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--shard-max-files", type=int, default=CODEBASE_SHARD_MAX_FILES)
    ap.add_argument("--shard-max-loc", type=int, default=CODEBASE_SHARD_MAX_LOC)
    args = ap.parse_args(normalize_argv(argv))
    try:
        out = build(args)
    except (ValueError, RuntimeError, OSError, carry_findings.BadInput) as exc:
        sys.stderr.write(f"build_bundle: error: {exc}\n")
        return 2
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
