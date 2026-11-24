# Effort: medium

## Medium

The goal is to spend review effort where most of the risk is (Pareto: the vital few findings), and to use fewer subagents.

### Bundle and intent
- Build the bundle as at `high`, but cap `context_files.txt` at 10 files or 1.5k LOC (`PR` only; at `CODEBASE`, `build_bundle.py` builds `file-map.json` and per-lane `.files.txt` lists / shards).
- Build the full intent pack.

### Lane selection
- **Cap: 3 lanes.** Keep the `high` priority order (`adherence` → `code-quality` → `conventions` → `security` → `performance` → `compatibility` → `robustness`, skipping unconditional `code-quality`/`conventions` when `lanes/selection.md` §Selection does not trigger them, with `persona.md` `emphasis` boosting matching conditional lanes).
- Use one `adherence` lane for all PR-relevant specs/plans (or shards at `CODEBASE`).
- Fold each dropped triggered lane into `conventions` (or `code-quality` if `conventions` is inactive), with one line per concern + its checklist path (`lanes/prompt.md`):
  - security: "also check authorization on each new or changed endpoint, trust of client input and headers, and data exposure across tenants or actors (`<skill_dir>/reference/lanes/checklist-security.md`)"
  - performance: "also check query count that grows with data, N+1, missing pagination, and repeated HTTP calls in loops (`<skill_dir>/reference/lanes/checklist-performance.md`)"
  - compatibility: "also check that public-contract changes are additive (serializers, OpenAPI, urls) and that migrations are rollout-safe (`<skill_dir>/reference/lanes/checklist-compatibility.md`)"
  - robustness: "also check async, transaction and side-effect ordering (`<skill_dir>/reference/lanes/checklist-robustness.md`)"
- Record each fold in the plan's `Skipped` line.

### Lane instructions (paste into every lane prompt as the `EFFORT` block)
```
EFFORT: medium (Pareto).
- First rank the files by risk: changed (at PR) or total (at CODEBASE) non-test lines, times 2 if the file matches one of these signals: <lead pastes the signals.txt lines and matching paths here>. Review the top files that hold about 80% of that score first. Review the rest only if time allows.
- Return every CRITICAL and HIGH finding. There is no cap on them.
- Return at most 5 MEDIUM, LOW or QUESTION findings: the ones with the highest impact × likelihood.
- Put the titles of any other findings you noticed in the "other" JSON array (titles only, no detail).
- adherence lane: evaluate every AC, task, FR/constraint, and P row, return only the rows that are not Met in "ac_matrix", and record the Met count in "coverage" (for example "12 Met rows").
```

The lead fills in the signal line, because a lane that reads only bundle files may not open `signals.txt`. At `medium`, `ac-matrix.md` holds the rows that the `adherence` lane returned, plus the count of Met rows from `"coverage"`.

### Verification and evaluation
- At most 2 blind verifiers (`standard` tier), batched by file via `scripts/merge_findings.py merge`. Verify only `CRITICAL` and `HIGH` findings, and `Missing` or `Contradicted` AC rows.
- Launch the Phase 2.2 `evaluation` subagent (`lanes/evaluation.md`, `standard` tier) **in parallel in the same message** as the blind verifiers on `nonblocking-candidates.json`.
- Run `scripts/merge_findings.py finalize <bundle_dir> --effort medium [--level=PR|CODEBASE] [--carry] [--still-open <file>] [--cleared <file>]` to enforce the safety floor, run Carry G9 (if `--carry`), invoke `breaker.py`, evaluate any `eval_followup_needed` inline, and compute the deterministic Pareto cut (`<bundle_dir>/pareto.json`).

### Pareto cut (computed deterministically by `scripts/merge_findings.py finalize --effort medium`)
1. Score each `todo` `post` finding: `score = severity weight × confidence weight × reach`.
   - Severity weight: CRITICAL 8, HIGH 5, MEDIUM 3, QUESTION 2, LOW 1.
   - Confidence weight: HIGH 1.0, MEDIUM 0.7, LOW 0.4.
   - Reach: 1.5 when the finding touches `breaking`, `ac_ref`, or a main/shared/auth/data/public path (`location`). Otherwise 1.0.
2. Sort by score, highest first, breaking ties by severity (and persona `emphasis` when supported).
3. Keep the smallest top set whose scores add up to at least 80% of the total score. Always keep every verified CRITICAL and HIGH, even past this point.
4. Report at most 10 non-blocking non-QUESTION findings and 3 QUESTION findings from the kept set, in addition to all confirmed CRITICAL and HIGH.
5. List the cut titles (`pareto.json` `cut_titles`) and the lanes' `"other"` titles in one line: "Other items that this review did not examine in depth: <a>, <b>, <c>." Keep this line in chat only. Do not post it to PRs.

### Quality gate
Compute it as at `high` on the lanes that ran. If any lane was folded into `conventions` (or `code-quality`), `breaker.py` appends `(partial)` after the band, so never show a plain `PASS`.
