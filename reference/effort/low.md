# Effort: low

## Low

The goal is a fast review that finds only CRITICAL and HIGH issues, with as little context as possible.

### Low context reduction (Phase 0)
- **Reduced diff.** Do not copy full files into `head/`, and do not build `context_files.txt`. Build `diff.core.patch` from the merge base with 5 lines of context:
  ```
  git -C <clone> diff -U5 --diff-filter=AMR <merge_base> <head_sha> -- . \
    ':(exclude,glob)**/*.spec.*' ':(exclude,glob)**/*.test.*' ':(exclude,glob)**/__tests__/**' \
    ':(exclude,glob)**/tests/**' ':(exclude,glob)**/test_*.py' ':(exclude,glob)**/*_test.py' \
    ':(exclude,glob)**/__snapshots__/**' ':(exclude,glob)**/locale/**' ':(exclude,glob)**/locales/**' \
    ':(exclude,glob)**/*.po' ':(exclude,glob)**/*.lock' ':(exclude,glob)**/package-lock.json' ':(exclude,glob)**/pnpm-lock.yaml' \
    ':(exclude,glob)**/*.min.js' ':(exclude,glob)**/generated/**' ':(exclude,glob)**/*.snap'
  ```
  Use anchored patterns like these. A pattern such as `*spec*` or `*test*` also matches paths like `inspection` or `attestation`, and so drops the core files.
- **Always keep** migrations, model files, permission and authentication code, URL routing, serializers, public API schemas (OpenAPI), and any paths required by CRITICAL or HIGH rules in `persona.md` §8–§9 in the reduced diff.
- **Read on demand.** When a hunk needs more context, read only a line range: `git -C <clone> show <head_sha>:<path> | sed -n '<a>,<b>p'`. Use `git grep` at `head_sha` to find callers. Never read a whole large file.
- **Compact intent pack.** Write `intent.md` with only: Sources, the Acceptance criteria / planned deliverables, the "must not change" statements from the objective boundaries, the PR claims, Conflicts and Gaps. Cite referenced spec items by ID and path, without verbatim excerpts. Run the planned document lookup steps of `intent.md` §3.3, and read only the sections that hold acceptance criteria, tasks and "must not change" statements. Read the PR description and the commit headlines, not each commit.
- **Quiet tools.** Cap tool output: use `wc -l`, `grep -c`, `head -n`, and `--stat`, and do not print a whole diff into the lead's context.

- **Keep for posting and Gate G2.** Still write `pr.json`, `files.txt`, `signals.txt`, the full `diff.patch`, and `diff-map.json` (`scripts/diff_map.py diff.patch > diff-map.json`) to disk, because Gate G2 (`scripts/schema_lint.py`) and Posting step 2 require them. Do not read `diff.patch` into context.

### Lane selection
- If `diff.core.patch` has fewer than 400 changed lines, the lead runs the review inline and launches no subagents.
- Otherwise, launch exactly one `critical-risk` lane (custom method, `lanes/critical-risk.md`) on **`fast`** model tier (unless `--no-tier` is set; fall back to `inherit` if unsupported). Do not launch any other lane.
- Use the `lanes/prompt.md` lane prompt template, but in BUNDLE list only `pr.json`, `files.txt`, `signals.txt`, `diff.core.patch` and `intent.md`. Include the wider code search block, because the lane reads file ranges on demand.

### Verification and evaluation
Run `scripts/schema_lint.py` against `diff-map.json` (G1–G4; G2 drops any out-of-diff finding). Then the lead verifies each CRITICAL and HIGH finding inline: re-read the cited line range at `head_sha` and one caller or guard. If the evidence does not hold, drop the finding. If it is not clear, downgrade the finding to a question. Do not launch verifier subagents. Then apply the Phase 2.3a `evaluation` rubric (`lanes/evaluation.md`) inline (routing low-signal observations to `chat-only` or `drop` while keeping all safety-floor findings at `post`) before `breaker.py --effort low`.

### Low report
Print this short form instead of `output/report.md` §Report. Write it in STE.
```markdown
## <🔴|🟢> <verdict phrase> — <owner>/<repo>#<N> · <PR title>
<one-sentence reason>

**Context:** head `<sha7>` · spec `<path>` @ `<sha7>` · tickets <...> · PR description <ok|one-line|empty>
**Gaps:** <list, or "none">. This review examined only CRITICAL and HIGH issues.

### Adherence
<only Missing and Contradicted rows, same table format; omit the section if there are none>

### Must fix (CRITICAL, HIGH)
<findings, same format as §Report>

### Questions for the author
1. **Q1** (`path:line`) — <at most 1 STE sentence explaining the item>. <Direct question?>   # at most 3

---
Effort: low · Lanes: <inline | critical-risk ✓> · Quality gate: n/a · Findings: <raw> raw → <kept> kept · Removed: <g2> out-of-diff, <eval> evaluated-out, <dropped> dropped by the lead · Persona: <refresh date>, <approved | unapproved (draft) | neutral (none)> · Language: ASD-STE100, G8 clean
```
Verdict phrases at `low`:
- Any verified CRITICAL, or a verified `Missing` or `Contradicted` AC: 🔴 "Not ready to merge".
- Any other HIGH: 🔴 "Fix the HIGH items before merge".
- Nothing: 🟢 "No CRITICAL or HIGH issues found. This review did not examine MEDIUM or LOW issues."

The posted event at `low` is always `COMMENT`. Never post `APPROVE` from a `low` review.
