#!/usr/bin/env python3
"""Parallel-threaded Phase 0 bundle builder, diff/codebase slicer, signal/effort resolver, and carry preparer.

Usage:
  build_bundle.py [target] [--level PR|CODEBASE] [--cwd <dir>] [--bundle-dir <dir>]
                  [--effort auto|high|medium|low|skim] [--since-last] [--fresh]
                  [--lanes a,b] [--no-inherit] [--inline] [--spec <path-or-url>]...
                  [--persona <path>] [--reviews-dir <dir>] [--local-only]

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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import carry_findings  # noqa: E402
import diff_map  # noqa: E402

SAFE_IDENT = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_NUM = re.compile(r"^[0-9]+$")
SAFE_SHA = re.compile(r"^[0-9a-f]{40}$")
UNSAFE_SHELL = re.compile(r'[\'"`$;|&<>\\\n\r]')

PR_URL_RX = re.compile(r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([0-9]+)(?:/.*)?$")
PR_SHORTHAND_RX = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)$")
PR_NUM_RX = re.compile(r"^#?([0-9]+)$")

TRACKER_KEY_RX = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
NON_TICKET_PREFIXES = (
    "UTF-", "ISO-", "SHA-", "RFC-", "HTTP-", "TLS-", "CVE-",
    "FR-", "NFR-", "AC-", "AD-", "OQ-", "PD-", "R-", "C-", "H-", "M-", "L-", "Q-",
)
GH_ISSUE_URL_RX = re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+")
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

LOCKFILE_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "Pipfile.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum", "uv.lock", "bun.lockb", "bun.lock",
}
MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile",
    "pom.xml", "composer.json", "requirements.txt",
}
DOC_EXTS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".eot", ".pdf", ".mp4", ".mp3", ".zip", ".gz", ".tar",
}
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts",
    ".rb", ".php", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".swift", ".scala", ".sh", ".bash",
    ".zsh", ".sql", ".vue", ".svelte", ".dart", ".ex", ".exs", ".clj", ".lua", ".pl", ".r",
}
LANG_MAP = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".cpp": "cpp", ".c": "c", ".swift": "swift",
    ".sql": "sql", ".vue": "vue", ".svelte": "svelte", ".md": "markdown", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".sh": "shell", ".css": "css", ".html": "html",
}

SIGNAL_RULES = {
    "auth": re.compile(
        r"\b(auth|authentication|authorization|authorisation|permission|policy|guard|middleware|"
        r"scope|token|jwt|oauth|session|role|acl|login_required|Authorize|PreAuthorize|before_action)\b",
        re.I,
    ),
    "input": re.compile(
        r"(controller|handler|serializer|dto|validator|upload|webhook|deseriali[sz]|raw_sql|"
        r"request\.body|req\.query|request\.data|params\[|SELECT\s+.+\+|execute\s*\()",
        re.I,
    ),
    "deps": re.compile(
        r"(package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|requirements.*\.txt|"
        r"pyproject\.toml|go\.mod|go\.sum|Cargo\.toml|Cargo\.lock|Gemfile|pom\.xml|"
        r"build\.gradle|composer\.json|\.csproj|Dockerfile|\.github/workflows/)",
        re.I,
    ),
    "data": re.compile(
        r"(migrations?/|db/migrate/|\.sql$|flyway|liquibase|\bmodels?\b|\bentities?\b|"
        r"\bALTER\s+TABLE\b|\bDROP\s+TABLE\b|bulk_update|bulk_delete|backfill)",
        re.I,
    ),
    "contract": re.compile(
        r"(routes?/|router|openapi|swagger|graphql|\.proto$|\.avsc$|/v[12]/|/public/|"
        r"serializer|schema|event_schema|feature_flag|featureFlag)",
        re.I,
    ),
    "perf": re.compile(
        r"(pagination|paginate|cache|redis|memcached|worker|background|batch|prefetch|"
        r"select_related|N\+1|useMemo|useCallback|for\s+.*:\s*.*(?:query|fetch|http|requests\.))",
        re.I,
    ),
    "async": re.compile(
        r"(\basync\b|\bawait\b|goroutine|\bgo\s+\w+|threading|mutex|\block\b|semaphore|"
        r"celery|sidekiq|bullmq|kafka|rabbitmq|transaction|on_commit|after_commit|retry|backoff|idempoten)",
        re.I,
    ),
    "ui": re.compile(r"(\.tsx$|\.jsx$|\.vue$|\.svelte$|\.css$|\.scss$|components?/|views?/|screens?/)", re.I),
}

REVIEW_LEVELS = ("PR", "CODEBASE")
MICRO_FOLD_MAX_LINES = 40
MICRO_FOLD_MAX_FILES = 2
MICRO_FOLDABLE_LANES = ("performance", "compatibility", "robustness")
CODEBASE_SHARD_MAX_FILES = 30
CODEBASE_SHARD_MAX_LOC = 6000
CODEBASE_SCAN_MAX_BYTES = 262144
WALK_SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".next", "target", ".idea", ".vscode",
}
CODEBASE_DOC_BASENAMES = {
    "README.md", "ARCHITECTURE.md", "DESIGN.md", "CONTRIBUTING.md", "SECURITY.md",
}


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
    if not p or p.startswith("/") or "\\" in p:
        return False
    parts = p.split("/")
    return ".." not in parts and "." not in parts


def is_denylisted(path):
    base = os.path.basename(path)
    low = path.lower()
    if base.startswith(".env") or low.endswith(".pem") or base in ("id_rsa", "id_ed25519", "credentials.json"):
        return True
    if "node_modules/" in path or base in LOCKFILE_NAMES or low.endswith(".lock"):
        return True
    _, ext = os.path.splitext(low)
    return ext in ASSET_EXTS


def is_test_path(path):
    base = os.path.basename(path)
    low = path.lower()
    if any(seg in low.split("/") for seg in ("tests", "test", "__tests__", "__snapshots__")):
        return True
    return bool(
        re.search(r"\.(?:spec|test)\.[^/]+$", base, re.I)
        or base.startswith("test_")
        or re.search(r"_test\.[^/]+$", base, re.I)
        or base.endswith(".snap")
    )


def is_generated_or_locale(path):
    low = path.lower()
    parts = low.split("/")
    if any(seg in parts for seg in ("generated", "locale", "locales", "__snapshots__", "dist", "build")):
        return True
    return low.endswith((".po", ".min.js", ".min.css", ".snap", ".lock")) or os.path.basename(path) in LOCKFILE_NAMES


def is_lock_or_manifest(path):
    base = os.path.basename(path)
    return (
        base in LOCKFILE_NAMES
        or base in MANIFEST_NAMES
        or base.endswith(".lock")
        or bool(re.match(r"^requirements.*\.txt$", base))
        or base.startswith("build.gradle")
        or base.endswith(".csproj")
    )


def is_rule_file(path):
    base = os.path.basename(path)
    low_base = base.lower()
    if base in RULE_BASENAMES or path in RULE_EXACT:
        return True
    if any(path.startswith(pref) or f"/{pref}" in path for pref in RULE_PREFIXES):
        return True
    return "security" in low_base and "checklist" in low_base


def is_spec_file(path):
    base = os.path.basename(path)
    if base in SPEC_BASENAMES or base.endswith((".spec.md", ".plan.md")):
        return True
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


def parse_diff_by_file(diff_text):
    """Split a unified diff into per-file sections with status and +/- line counts."""
    sections = []
    cur = None
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur:
                sections.append(cur)
            m = re.match(r"^diff --git a/(.+?) b/(.+?)\s*$", line)
            old_p = m.group(1) if m else ""
            new_p = m.group(2) if m else ""
            cur = {
                "old_path": old_p, "path": new_p, "status": "modified",
                "adds": 0, "dels": 0, "binary": False, "lines": [line],
            }
            continue
        if cur is None:
            continue
        cur["lines"].append(line)
        if line.startswith("new file mode "):
            cur["status"] = "added"
        elif line.startswith("deleted file mode "):
            cur["status"] = "deleted"
        elif line.startswith("rename from "):
            cur["status"] = "renamed"
            cur["old_path"] = line[len("rename from "):].strip()
        elif line.startswith("rename to "):
            cur["path"] = line[len("rename to "):].strip()
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("+") and not line.startswith("+++ "):
            cur["adds"] += 1
        elif line.startswith("-") and not line.startswith("--- "):
            cur["dels"] += 1
    if cur:
        sections.append(cur)
    for s in sections:
        s["text"] = "".join(s["lines"])
        s["lang"] = lang_of(s["path"])
        del s["lines"]
    return sections


def parse_persona_frontmatter(persona_path):
    """Parse persona.md frontmatter + detect active §8/§9 rules without external YAML libs."""
    info = {
        "status": "empty", "refreshed": None, "memory": "ask",
        "emphasis": [], "has_house_rules": False, "has_learned_prefs": False,
        "voice": {"tone": "direct", "max_inline": 8, "hold_event": "REQUEST_CHANGES"},
    }
    if not persona_path or not os.path.isfile(persona_path):
        return info
    text = open(persona_path, encoding="utf-8", errors="replace").read()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if m:
        fm, body = m.group(1), m.group(2)
        in_emphasis = False
        for raw in fm.splitlines():
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if re.match(r"^emphasis:\s*\[(.*)\]\s*$", line):
                items = [x.strip().strip("'\"") for x in re.match(r"^emphasis:\s*\[(.*)\]", line).group(1).split(",") if x.strip()]
                info["emphasis"] = items
                in_emphasis = False
            elif re.match(r"^emphasis:\s*$", line):
                in_emphasis = True
            elif in_emphasis and re.match(r"^\s+-\s+(.+)$", line):
                info["emphasis"].append(re.match(r"^\s+-\s+(.+)$", line).group(1).strip().strip("'\""))
            else:
                in_emphasis = False
                kv = re.match(r"^(\s*)([A-Za-z0-9_]+):\s*(.+?)\s*$", line)
                if kv:
                    indent, k, v = len(kv.group(1)), kv.group(2), kv.group(3).strip("'\"")
                    if indent == 0:
                        if k == "status":
                            info["status"] = v
                        elif k == "refreshed":
                            info["refreshed"] = None if v == "null" else v
                        elif k == "memory":
                            info["memory"] = v
                    elif k == "tone":
                        info["voice"]["tone"] = v
                    elif k == "max_inline" and v.isdigit():
                        info["voice"]["max_inline"] = int(v)
                    elif k == "hold_event":
                        info["voice"]["hold_event"] = v
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


def find_clone(cwd, owner=None, repo=None):
    candidates = []
    rc, top, _ = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if rc == 0 and top.strip():
        candidates.append(top.strip())
    if repo:
        candidates.append(os.path.join(cwd, repo))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(cwd)), repo))
    for cand in candidates:
        if not os.path.isdir(cand):
            continue
        if not owner or not repo:
            return cand
        rc, url, _ = run_cmd(["git", "-C", cand, "remote", "get-url", "origin"])
        if rc == 0 and f"{owner}/{repo}" in url:
            return cand
    return None


def parse_target(target, cwd):
    """Return (mode, owner, repo, number, url) or raise ValueError on unsafe input."""
    if target is None or not str(target).strip():
        return "auto", None, None, None, None
    t = str(target).strip()
    if UNSAFE_SHELL.search(t):
        raise ValueError("unsafe characters in target argument")
    m = PR_URL_RX.match(t)
    if m:
        owner, repo, num = m.group(1), m.group(2), m.group(3)
        return "pr", owner, repo, num, f"https://github.com/{owner}/{repo}/pull/{num}"
    m = PR_SHORTHAND_RX.match(t)
    if m:
        owner, repo, num = m.group(1), m.group(2), m.group(3)
        return "pr", owner, repo, num, f"https://github.com/{owner}/{repo}/pull/{num}"
    m = PR_NUM_RX.match(t)
    if m:
        num = m.group(1)
        rc, url, _ = run_cmd(["git", "remote", "get-url", "origin"], cwd=cwd)
        if rc != 0:
            raise ValueError("cannot infer owner/repo for #N without git origin")
        rm = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$", url.strip())
        if not rm:
            raise ValueError("origin remote is not a GitHub repository")
        owner, repo = rm.group(1), rm.group(2)
        return "pr", owner, repo, num, f"https://github.com/{owner}/{repo}/pull/{num}"
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
        if concl in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") or state in ("ERROR", "FAILURE"):
            failing.append(name)
        elif state in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING") or (not concl and state != "SUCCESS"):
            pending = True
    if failing:
        return "failing: " + ", ".join(failing[:5])
    if pending:
        return "pending"
    return "passing"


def detect_signals(sections):
    signals = {}
    for sec in sections:
        path = sec["path"]
        added_lines = "\n".join(
            ln[1:] for ln in sec["text"].splitlines()
            if ln.startswith("+") and not ln.startswith("+++ ")
        )
        haystack = path + "\n" + added_lines
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
        if sec["binary"]:
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


def select_lanes(effort, signals, lane_manifest, persona, rules_changed, args, small_pr):
    """Apply conditional triggers, micro-lane folding, effort caps, overrides, and tiering."""
    if effort == "skim":
        tier = "standard" if args.no_tier else "fast"
        return {
            "selected": [{"lane": "skim", "tier": tier, "why": "skim profile"}],
            "folded": [], "skipped": [], "inline_adherence": False,
        }
    if effort == "low":
        core_lines = sum(
            s["changed_lines"] for k, s in lane_manifest.items() if k == "code-quality"
        )
        if core_lines < 400 and not args.lanes:
            return {
                "selected": [{"lane": "critical-risk", "tier": "inline", "why": "low effort under 400 lines"}],
                "folded": [], "skipped": [], "inline_adherence": True,
            }
        tier = "standard" if args.no_tier else "fast"
        return {
            "selected": [{"lane": "critical-risk", "tier": tier, "why": "low effort single lane"}],
            "folded": [], "skipped": [], "inline_adherence": False,
        }

    # high and medium always run all lanes on standard (inherit) tier
    emphasis = {e.lower() for e in persona.get("emphasis") or []}
    skipped = []
    folded = []

    if args.lanes:
        req = [x.strip() for x in args.lanes.split(",") if x.strip()]
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
        for ln, _ in rem[2:]:
            if ln in ("security", "performance", "compatibility", "robustness") and fold_target:
                folded.append({"lane": ln, "into": fold_target, "why": "small-PR cap fold"})
                skipped.append({"lane": ln, "why": f"folded into {fold_target} (small-PR mode)"})
            else:
                skipped.append({"lane": ln, "why": "small-PR cap"})
        return {
            "selected": [{"lane": "adherence", "tier": "inline", "why": "small-PR inline adherence"}]
            + [{"lane": ln, "tier": "standard", "why": w} for ln, w in kept_rem],
            "folded": folded, "skipped": skipped, "inline_adherence": True,
        }

    kept = candidates[:cap]
    overflow = candidates[cap:]
    for ln, _ in overflow:
        if fold_target and ln in ("security", "performance", "compatibility", "robustness"):
            folded.append({"lane": ln, "into": fold_target, "why": f"{effort} effort lane cap ({cap})"})
            skipped.append({"lane": ln, "why": f"folded into {fold_target} (cap {cap})"})
        else:
            skipped.append({"lane": ln, "why": f"exceeded cap ({cap})"})

    return {
        "selected": [{"lane": ln, "tier": "standard", "why": w} for ln, w in kept],
        "folded": folded, "skipped": skipped, "inline_adherence": False,
    }


def extract_intent_hints(pr_data, sections, clone, head_sha, cli_specs):
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

    gh_issues = sorted(set(GH_ISSUE_URL_RX.findall(blob)))
    for ref in pr_data.get("closingIssuesReferences") or []:
        if isinstance(ref, dict) and ref.get("url"):
            if ref["url"] not in gh_issues:
                gh_issues.append(ref["url"])

    external_urls = []
    for u in sorted(set(URL_RX.findall(body))):
        if "/pull/" in u or "/issues/" in u:
            continue
        external_urls.append(u)

    diff_specs = [s["path"] for s in sections if is_spec_file(s["path"])]
    repo_specs = []
    if clone and head_sha and SAFE_SHA.match(head_sha):
        rc, out, _ = run_cmd(["git", "-C", clone, "ls-tree", "-r", "--name-only", head_sha])
        if rc == 0:
            for p in out.splitlines():
                if is_spec_file(p) and p not in diff_specs:
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


def find_previous_findings(reviews_dir, owner, repo, number):
    if not reviews_dir or not os.path.isdir(reviews_dir):
        return None
    prefix = f"{owner}-{repo}-{number}-"
    matches = []
    for name in os.listdir(reviews_dir):
        if not name.startswith(prefix):
            continue
        fp = os.path.join(reviews_dir, name, "findings.json")
        if os.path.isfile(fp):
            try:
                doc = json.load(open(fp, encoding="utf-8"))
                has_next = isinstance(doc, dict) and "next_id" in doc
                matches.append((has_next, os.path.getmtime(fp), fp))
            except (OSError, ValueError):
                continue
    if not matches:
        return None
    matches.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return matches[0][2]


def copy_snapshot_files(clone, head_sha, paths, out_root, owner=None, repo=None):
    """Copy repo files at head_sha into out_root in parallel threads."""
    copied = []
    failed = []

    def fetch_one(path):
        if not is_safe_relpath(path) or is_denylisted(path):
            return path, False
        dst = os.path.join(out_root, path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if clone:
            rc, content, _ = run_cmd(["git", "-C", clone, "show", f"{head_sha}:{path}"])
        elif owner and repo:
            rc, content, _ = run_cmd([
                "gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={head_sha}",
                "-H", "Accept: application/vnd.github.raw",
            ])
        else:
            return path, False
        if rc != 0:
            return path, False
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
        rc, out, _ = run_cmd(["git", "-C", clone, "grep", "-l", "-F", base, head_sha])
        if rc == 0:
            for m in out.splitlines():
                if ":" in m:
                    m = m.split(":", 1)[1]
                if m not in changed_set and is_safe_relpath(m) and not is_denylisted(m):
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
        if rc != 0:
            continue
        loc = content.count("\n") + 1
        if total_loc + loc > max_loc and selected:
            continue
        selected.append((path, content))
        total_loc += loc
    return selected


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
        # Split an oversized single directory into bounded chunks
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

    by_lane = {
        "adherence": specs_docs if specs_docs else non_test_code,
        "code-quality": all_code,
        "conventions": order_priority(rules + non_test_code, {r["path"] for r in rules}),
        "security": order_priority(non_test_code + manifests_ci, sec_prio),
        "performance": order_priority(non_test_code, perf_prio),
        "compatibility": order_priority(non_test_code + manifests_ci, compat_prio) if (non_test_code or compat_prio) else [],
        "robustness": order_priority(non_test_code, rob_prio),
    }
    prio_by_lane = {
        "adherence": {r["path"] for r in specs_docs},
        "code-quality": set(),
        "conventions": {r["path"] for r in rules},
        "security": sec_prio,
        "performance": perf_prio,
        "compatibility": compat_prio,
        "robustness": rob_prio,
    }

    manifest = {}
    file_lists = {}
    for lane, recs in by_lane.items():
        lines = sum(r["lines"] for r in recs)
        prio_set = prio_by_lane[lane]
        prio_files = [r["path"] for r in recs if r["path"] in prio_set]
        shards = shard_codebase_records(lane, recs, max_shard_files, max_shard_loc)
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
        total_code_lines = lane_manifest["code-quality"]["total_lines"]
        if total_code_lines < 400 and not args.lanes:
            return {
                "selected": [{"lane": "critical-risk", "tier": "inline", "why": "low effort codebase under 400 lines"}],
                "folded": [], "skipped": [], "inline_adherence": True,
            }
        tier = "standard" if args.no_tier else "fast"
        return {
            "selected": [{"lane": "critical-risk", "tier": tier, "why": "low effort codebase single lane"}],
            "folded": [], "skipped": [], "inline_adherence": False,
        }

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
        req = [x.strip() for x in args.lanes.split(",") if x.strip()]
        selected = []
        for ln in req:
            selected.extend(expand(ln, "explicit --lanes override"))
        return {"selected": selected, "folded": [], "skipped": [], "inline_adherence": False}

    skipped = []
    folded = []
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
    # Preserve priority order; boost emphasized conditional lanes when medium cap applies
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
    overflow = candidates[cap:]
    active_names = {c[0] for c in kept}
    fold_target = "conventions" if "conventions" in active_names else ("code-quality" if "code-quality" in active_names else None)
    for ln, _ in overflow:
        if fold_target and ln in ("security", "performance", "compatibility", "robustness"):
            folded.append({"lane": ln, "into": fold_target, "why": f"{effort} effort lane cap ({cap})"})
            skipped.append({"lane": ln, "why": f"folded into {fold_target} (cap {cap})"})
        else:
            skipped.append({"lane": ln, "why": f"exceeded cap ({cap})"})

    selected = []
    for ln, why in kept:
        selected.extend(expand(ln, why))
    return {"selected": selected, "folded": folded, "skipped": skipped, "inline_adherence": False}


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
        rm = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$", url.strip()) if rc_u == 0 else None
        owner = rm.group(1) if rm else "local"
        repo = rm.group(2) if rm else os.path.basename(os.path.abspath(clone))

        rc_ls, out_ls, err_ls = run_cmd(["git", "-C", clone, "ls-tree", "-r", "--name-only", head_sha])
        if rc_ls != 0:
            raise RuntimeError(f"git ls-tree failed: {err_ls.strip()}")
        raw_paths = [p for p in out_ls.splitlines() if p.strip()]
    else:
        head_sha = "working-tree"
        sha7 = "worktree"
        head_ref = "working-tree"
        owner = "local"
        repo = os.path.basename(cwd)
        coverage_limits.append("not a git repository — working tree scanned directly")
        raw_paths = []
        for root, dirs, files in os.walk(cwd):
            dirs[:] = sorted(d for d in dirs if d not in WALK_SKIP_DIRS)
            for fn in sorted(files):
                rel = os.path.relpath(os.path.join(root, fn), cwd).replace(os.sep, "/")
                raw_paths.append(rel)

    if scope_path:
        raw_paths = [p for p in raw_paths if p == scope_path or p.startswith(scope_path + "/")]

    candidate_paths = []
    for p in sorted(set(raw_paths)):
        if not is_safe_relpath(p) or is_denylisted(p):
            continue
        if is_generated_or_locale(p):
            continue
        candidate_paths.append(p)

    def inspect_one(path):
        if clone:
            rc, content, _ = run_cmd(["git", "-C", clone, "show", f"{head_sha}:{path}"])
            if rc != 0:
                return None
        else:
            try:
                with open(os.path.join(cwd, path), encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                return None
        if "\x00" in content[:8192]:
            return None
        lines = len(content.splitlines())
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
            "is_doc": is_doc_or_asset(path),
            "is_gen": is_generated_or_locale(path),
            "signals": matched_sigs,
        }

    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(inspect_one, candidate_paths):
            if res is not None:
                records.append(res)

    if args.bundle_dir:
        bundle_dir = os.path.abspath(args.bundle_dir)
    else:
        job_dir = os.environ.get("CLAUDE_JOB_DIR")
        base_tmp = os.path.join(job_dir, "tmp") if job_dir else tempfile.gettempdir()
        bundle_dir = os.path.join(base_tmp, "review", f"{owner}-{repo}-codebase-{sha7}")
    os.makedirs(bundle_dir, exist_ok=True)

    # Carry check against previous codebase reviews (never delta_mode)
    carry_info = {"enabled": False, "delta_mode": False, "previous_path": None, "history_rewritten": False}
    prev_path = None if args.fresh else find_previous_findings(args.reviews_dir, owner, repo, "codebase")
    if prev_path:
        state, _, warnings = carry_findings.load_previous(prev_path)
        declines = [carry_findings.decline_row(f) for f in state["findings"] if f["status"] == "declined"]
        open_ = [carry_findings.open_row(f) for f in state["findings"] if f["status"] == "todo" and f["severity"] in carry_findings.BLOCKING]
        carry_findings.write_json(os.path.join(bundle_dir, "previous.json"), state)
        carry_findings.write_json(os.path.join(bundle_dir, "declines.json"), declines)
        carry_findings.write_json(os.path.join(bundle_dir, "open.json"), open_)
        carry_info.update({
            "enabled": True, "previous_path": prev_path, "last_sha": state.get("last_sha"),
            "declines_count": len(declines), "open_count": len(open_), "warnings": warnings,
        })

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

    max_sf = getattr(args, "shard_max_files", CODEBASE_SHARD_MAX_FILES) or CODEBASE_SHARD_MAX_FILES
    max_sl = getattr(args, "shard_max_loc", CODEBASE_SHARD_MAX_LOC) or CODEBASE_SHARD_MAX_LOC
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
        "persona": {
            "status": persona["status"], "refreshed": persona["refreshed"],
            "memory": persona["memory"], "voice": persona["voice"],
            "has_rules": bool(persona["has_house_rules"] or persona["has_learned_prefs"]),
        },
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
                if view.get("url"):
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
        pr_data = json.loads(out_pr)
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
        base_ref_oid = str(pr_data.get("baseRefOid") or "")
        base_ref = str(pr_data.get("baseRefName") or "")

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
            if rc_ic == 0 and out_ic.strip():
                for chunk in out_ic.splitlines():
                    try:
                        parsed = json.loads(chunk)
                        if isinstance(parsed, list):
                            inline_comments.extend(parsed)
                    except ValueError:
                        pass
                if not inline_comments:
                    try:
                        parsed = json.loads(out_ic)
                        if isinstance(parsed, list):
                            inline_comments = parsed
                    except ValueError:
                        pass

            rc_rv, out_rv, _ = f_rc.result()
            if rc_rv == 0 and out_rv.strip():
                try:
                    parsed = json.loads(out_rv)
                    if isinstance(parsed, list):
                        review_comments = parsed
                except ValueError:
                    pass

            if f_fetch_head:
                f_fetch_head.result()
            if f_fetch_base:
                f_fetch_base.result()

        # Resolve merge base
        base_sha = "unavailable"
        if clone and SAFE_SHA.match(head_sha) and SAFE_SHA.match(base_ref_oid):
            rc_mb, out_mb, _ = run_cmd(["git", "-C", clone, "merge-base", base_ref_oid, head_sha])
            if rc_mb == 0 and SAFE_SHA.match(out_mb.strip()):
                base_sha = out_mb.strip()
            else:
                base_sha = base_ref_oid
                coverage_limits.append("base is the branch tip, not the merge base")
        elif SAFE_SHA.match(head_sha) and SAFE_SHA.match(base_ref_oid):
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

        # Find default base ref
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

        rc_d, out_d, err_d = run_cmd(["git", "-C", clone, "diff", f"{base_sha}..{head_sha}"])
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
    if args.bundle_dir:
        bundle_dir = os.path.abspath(args.bundle_dir)
    else:
        job_dir = os.environ.get("CLAUDE_JOB_DIR")
        base_tmp = os.path.join(job_dir, "tmp") if job_dir else tempfile.gettempdir()
        bundle_dir = os.path.join(base_tmp, "review", f"{owner}-{repo}-{number}-{sha7}")
    os.makedirs(bundle_dir, exist_ok=True)

    # Carry & delta check
    carry_info = {"enabled": False, "delta_mode": False, "previous_path": None, "history_rewritten": False}
    prev_path = None if args.fresh else find_previous_findings(args.reviews_dir, owner, repo, number)
    full_pr_diff = diff_text
    if prev_path:
        state, id_map, warnings = carry_findings.load_previous(prev_path)
        declines = [carry_findings.decline_row(f) for f in state["findings"] if f["status"] == "declined"]
        open_ = [carry_findings.open_row(f) for f in state["findings"] if f["status"] == "todo" and f["severity"] in carry_findings.BLOCKING]
        carry_findings.write_json(os.path.join(bundle_dir, "previous.json"), state)
        carry_findings.write_json(os.path.join(bundle_dir, "declines.json"), declines)
        carry_findings.write_json(os.path.join(bundle_dir, "open.json"), open_)
        carry_info.update({
            "enabled": True, "previous_path": prev_path, "last_sha": state.get("last_sha"),
            "declines_count": len(declines), "open_count": len(open_), "warnings": warnings,
        })
        last_sha = state.get("last_sha")
        if clone and last_sha and SAFE_SHA.match(last_sha) and (last_sha != head_sha or args.since_last):
            rc_anc, _, _ = run_cmd(["git", "-C", clone, "merge-base", "--is-ancestor", last_sha, head_sha])
            if rc_anc == 0:
                rc_dd, out_dd, _ = run_cmd(["git", "-C", clone, "diff", f"{last_sha}..{head_sha}"])
                if rc_dd == 0:
                    with open(os.path.join(bundle_dir, "pr.diff.patch"), "w", encoding="utf-8") as fh:
                        fh.write(full_pr_diff)
                    diff_text = out_dd
                    carry_info["delta_mode"] = True
            else:
                carry_info["history_rewritten"] = True

    # Write pr.json, diff.patch, existing-comments.json, diff-map.json
    with open(os.path.join(bundle_dir, "pr.json"), "w", encoding="utf-8") as fh:
        json.dump(pr_data, fh, indent=2, ensure_ascii=False)
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

    intent_hints = extract_intent_hints(pr_data, sections, clone, head_sha, args.spec)
    total_adds = sum(s["adds"] for s in sections)
    total_dels = sum(s["dels"] for s in sections)
    core_lines = sum(
        s["adds"] + s["dels"] for s in sections
        if not is_test_path(s["path"]) and not is_generated_or_locale(s["path"]) and not is_lock_or_manifest(s["path"])
    )
    skim_lines = sum(
        s["adds"] + s["dels"] for s in sections
        if not is_generated_or_locale(s["path"]) and not is_lock_or_manifest(s["path"])
    )
    lockfile_only = bool(sections) and all(is_lock_or_manifest(s["path"]) for s in sections)
    author_login = str((pr_data.get("author") or {}).get("login") or "").lower()
    is_bot = any(b in author_login for b in ("dependabot", "renovate", "[bot]", "bot")) or bool((pr_data.get("author") or {}).get("is_bot"))
    spec_count = len(intent_hints["cli_specs"]) + len(intent_hints["diff_specs"])
    in_skim_profile = (
        skim_lines < 100
        and len(sections) <= 4
        and spec_count == 0
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
    changed_rules = [s for s in sections if is_rule_file(s["path"])]
    rule_records = []
    if changed_rules:
        base_root = os.path.join(bundle_dir, "base")
        for idx, rs in enumerate(changed_rules):
            p = rs["path"]
            st = rs["status"]
            if st == "added":
                bstate = "none"
            elif effort in ("low", "skim"):
                bstate = "skipped"
            elif idx >= 30 or base_sha == "unavailable" or not is_safe_relpath(p):
                bstate = "unavailable"
            else:
                src_p = rs["old_path"] if st == "renamed" else p
                if clone:
                    rc_b, content_b, _ = run_cmd(["git", "-C", clone, "show", f"{base_sha}:{src_p}"])
                else:
                    rc_b, content_b, _ = run_cmd([
                        "gh", "api", f"repos/{owner}/{repo}/contents/{src_p}?ref={base_sha}",
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

    # Reduced diffs for low / skim
    if effort == "low":
        core_patch = "".join(
            s["text"] for s in sections
            if not is_test_path(s["path"]) and not is_generated_or_locale(s["path"]) and not is_lock_or_manifest(s["path"])
        )
        with open(os.path.join(bundle_dir, "diff.core.patch"), "w", encoding="utf-8") as fh:
            fh.write(core_patch)
    elif effort == "skim":
        skim_patch = "".join(
            s["text"] for s in sections
            if not is_generated_or_locale(s["path"]) and not is_lock_or_manifest(s["path"])
        )
        lock_patch = "".join(s["text"] for s in sections if is_lock_or_manifest(s["path"]))
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
        copy_snapshot_files(clone, head_sha, copy_targets, head_root, owner, repo)

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
    lane_plan = select_lanes(effort, signals, lane_manifest, persona, rule_records, args, small_pr)

    summary = {
        "level": "PR",
        "gate_g0": "pass",
        "target_mode": target_mode,
        "bundle_dir": bundle_dir,
        "clone": clone,
        "pr": {
            "owner": owner, "repo": repo, "number": int(number) if str(number).isdigit() else 0,
            "url": pr_url, "title": pr_data.get("title"), "head_sha": head_sha, "sha7": sha7,
            "base_sha": base_sha, "base_ref": pr_data.get("baseRefName"),
            "author_is_me": author_is_me, "is_draft": bool(pr_data.get("isDraft")),
            "ci": compute_ci(pr_data.get("statusCheckRollup")),
            "additions": total_adds, "deletions": total_dels, "changed_files": len(sections),
            "core_changed_lines": core_lines, "skim_changed_lines": skim_lines,
        },
        "gate_g0b": {
            "draft": bool(pr_data.get("isDraft")), "bot": is_bot,
            "lockfile_only": lockfile_only, "in_skim_profile": in_skim_profile,
        },
        "effort": {"resolved": effort, "reason": effort_reason, "small_pr": small_pr},
        "persona": {
            "status": persona["status"], "refreshed": persona["refreshed"],
            "memory": persona["memory"], "voice": persona["voice"],
            "has_rules": bool(persona["has_house_rules"] or persona["has_learned_prefs"]),
        },
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
    default_reviews = os.path.expanduser("~/.claude/review/reviews")
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
    ap.add_argument("--reviews-dir", default=default_reviews)
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--shard-max-files", type=int, default=CODEBASE_SHARD_MAX_FILES)
    ap.add_argument("--shard-max-loc", type=int, default=CODEBASE_SHARD_MAX_LOC)
    args = ap.parse_args(normalize_argv(argv))
    try:
        out = build(args)
    except (ValueError, RuntimeError) as exc:
        sys.stderr.write(f"build_bundle: error: {exc}\n")
        return 2
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
