# Effort: low

## Low

The goal is a fast review that finds only CRITICAL and HIGH issues, with as little context as possible.

### Low context reduction (Phase 0, `PR` mode; at `CODEBASE`, see `reference/codebase.md`)
- **Reduced diff.** `build_bundle.py` skips `head/` copies and `context_files.txt`, and builds `diff.core.patch` keeping manifests, code, migrations, models, auth/permission code, routing, serializers, and OpenAPI schemas while excluding tests, lockfiles, generated/locale files, and snapshots:
  ```
  git -C <clone> diff -U3 <merge_base> <head_sha> -- . \
    ':(exclude,glob)**/*.spec.*' ':(exclude,glob)**/*.test.*' ':(exclude,glob)**/__tests__/**' \
    ':(exclude,glob)**/tests/**' ':(exclude,glob)**/test_*.py' ':(exclude,glob)**/*_test.py' \
    ':(exclude,glob)**/__snapshots__/**' ':(exclude,glob)**/locale/**' ':(exclude,glob)**/locales/**' \
    ':(exclude,glob)**/*.po' ':(exclude,glob)**/*.lock' ':(exclude,glob)**/package-lock.json' ':(exclude,glob)**/pnpm-lock.yaml' \
    ':(exclude,glob)**/yarn.lock' ':(exclude,glob)**/poetry.lock' ':(exclude,glob)**/Pipfile.lock' ':(exclude,glob)**/Cargo.lock' \
    ':(exclude,glob)**/Gemfile.lock' ':(exclude,glob)**/composer.lock' ':(exclude,glob)**/go.sum' ':(exclude,glob)**/uv.lock' \
    ':(exclude,glob)**/bun.lockb' ':(exclude,glob)**/bun.lock' ':(exclude,glob)**/*.min.js' ':(exclude,glob)**/generated/**' ':(exclude,glob)**/*.snap'
  ```
  Use anchored patterns like these (do not pass `--diff-filter=AMR`, which drops deleted files). A pattern such as `*spec*` or `*test*` also matches paths like `inspection` or `attestation`, and so drops core files.
- **Always keep** manifests, migrations, model files, permission and authentication code, URL routing, serializers, public API schemas (OpenAPI), and any paths required by CRITICAL or HIGH rules in `persona.md` §8–§9 in the reduced diff.
- **Read on demand.** When a hunk needs more context, read only a line range: `git -C <clone> show <head_sha>:<path> | sed -n '<a>,<b>p'`. Use `git grep` at `head_sha` to find callers. Never read a whole large file.
- **Compact intent pack.** Write `intent.md` with only: Sources, the Acceptance criteria / planned deliverables, the "must not change" statements from the objective boundaries, the PR claims, Conflicts and Gaps. Cite referenced spec items by ID and path, without verbatim excerpts. Run the planned document lookup steps of `intent.md` §3.3, and read only the sections that hold acceptance criteria, tasks and "must not change" statements. Read the PR description and the commit headlines, not each commit.
- **Quiet tools.** Cap tool output: use `wc -l`, `grep -c`, `head -n`, and `--stat`, and do not print a whole diff into the lead's context.
- **Keep for posting and Gate G2.** `build_bundle.py` still writes `pr.json`, `files.txt`, `signals.txt`, the full `diff.patch`, and `diff-map.json` (`scripts/diff_map.py diff.patch > diff-map.json`) to disk, because Gate G2 (`scripts/merge_findings.py` / `scripts/schema_lint.py`) and Posting step 2 require them. Do not read `diff.patch` into context.

### Lane selection
- At `PR`, if `core_lines < 400` and `--lanes` is not set (`build_bundle.py` `lane_plan.selected[0].tier == "inline"`), the lead runs `critical-risk` inline and launches no subagents.
- Otherwise (or whenever `--lanes` is set), launch exactly one `critical-risk` lane (custom method, `lanes/critical-risk.md`) on **`fast`** model tier (unless `--no-tier` is set; fall back to `inherit` if unsupported). Do not launch any other lane.
- At `CODEBASE --effort=low`, `build_bundle.py` prepares `lanes/critical-risk.files.txt` (`tier: inline` when `code_lines < 400` and no `--lanes`, else `fast` unless `--no-tier`), and the `CODEBASE` template in `lanes/prompt.md` + `reference/codebase.md` applies.
- At `PR`, use the `lanes/prompt.md` lane prompt template with `pr.json`, `files.txt`, `signals.txt`, `diff.core.patch`, and `intent.md` in `BUNDLE` (plus `declines.json` and `open.json` whenever they exist in `bundle_dir`). Include the wider code search block, because the lane reads file ranges on demand.

### Verification and evaluation
Save the `critical-risk` JSON output (subagent or inline) to `<bundle_dir>/raw-lanes.json` and run `python3 <skill_dir>/scripts/merge_findings.py merge <bundle_dir> [--level=PR|CODEBASE]` (Gates G1–G5; G2 drops out-of-diff or invalid-location items). Then the lead verifies each CRITICAL and HIGH finding inline: re-read the cited line range at `head_sha` and one caller or guard, writing `<bundle_dir>/verifier-results.json` (`"refuted"` if evidence fails, `"unclear"` to downgrade to a question, `"confirmed"` if verified). Apply the Phase 2.2 `evaluation` rubric (`lanes/evaluation.md`) inline (keeping all confirmed safety-floor findings at `post` and dropping any non-blocking items outside scope/questions) into `<bundle_dir>/eval-results.json`, then run `python3 <skill_dir>/scripts/merge_findings.py finalize <bundle_dir> [--level=PR|CODEBASE] [--carry] [--still-open <file>] [--cleared <file>]`.

### Low report
At `level=CODEBASE`, use the codebase report template in `reference/codebase.md`. At `level=PR`, print this short form instead of `output/report.md` §Report. Write it in STE.
```markdown
## <🔴|🟡|🟢> <verdict phrase> — <owner>/<repo>#<N> · <PR title>
<one-sentence reason>

**Context:** head `<sha7>` · spec `<path>` @ `<sha7>` · tickets <...> · PR description <ok|one-line|empty>.
**Gaps:** <list, or "none">. This review examined only CRITICAL and HIGH issues.
<include **High risk:** / **Review safety:** / **Earlier declines still present:** / ### Earlier findings when non-empty>

### Adherence
<only Missing and Contradicted rows, same table format; omit the section if there are none>

### Must fix (CRITICAL, HIGH)
<findings, same format as §Report>

### Questions for the author
1. **Q1** (`path:line`) — <1 concise STE sentence explaining the item>. <Direct question?>   # at most 3

---
Effort: low · Lanes: <inline | critical-risk ✓> · Quality gate: n/a · Findings: <raw> raw → <kept> post · Gates removed: <g2> out-of-diff, <g5> already raised, <refuted> refuted, <eval> evaluated-out · Persona: <refresh date>, approved | <refresh date>, draft | neutral (none) · Language: ASD-STE100, G8 clean
```
Verdict phrases at `low` (computed by `breaker.py --effort low`):
- Any verified CRITICAL, or a verified `Missing` or `Contradicted` AC: 🔴 `"Not ready to merge"`.
- Any other HIGH: 🔴 `"Fix the HIGH items before merge"`.
- Capped from clean by `declined_blocking` or `NOT ASSESSED`: 🟡 `"Ready to merge after the comments"`.
- Nothing at CRITICAL or HIGH: 🟢 `"No CRITICAL or HIGH issues found. This review did not examine MEDIUM or LOW issues."`
- When `author_is_me` is true: `breaker.py` overrides the phrase to `"Not ready for review"` (blocking) or `"Ready for review"` (otherwise).

The posted event at `low` is always `COMMENT`. Never post `APPROVE` from a `low` review.
