# Effort: medium

## Medium

The goal is to spend review effort where most of the risk is (Pareto: the vital few findings), and to use fewer subagents.

### Bundle and intent
- Build the bundle as at `high`, but cap `context_files.txt` at 10 files or 1.5k LOC.
- Build the full intent pack.

### Lane selection
- **Cap: 3 lanes.** Keep the `high` priority order (`adherence` → `code-quality` → `conventions` → `security` → `performance` → `compatibility` → `robustness`, skipping unconditional `code-quality`/`conventions` when `lanes/selection.md` §Selection does not trigger them, with `persona.md` `emphasis` boosting matching conditional lanes).
- Use one `adherence` lane for all PR-relevant specs/plans.
- Fold each dropped triggered lane into `conventions` (if `conventions` is running, which can read wider), with one line per concern:
  - security: "also check authorization on each new or changed endpoint, trust of client input and headers, and data exposure across tenants or actors"
  - performance: "also check query count that grows with data, N+1, missing pagination, and repeated HTTP calls in loops"
  - compatibility: "also check that public-contract changes are additive (serializers, OpenAPI, urls) and that migrations are rollout-safe"
  - robustness: "also check async, transaction and side-effect ordering"
- Record each fold in the plan's `Skipped` line.

### Lane instructions (paste into every lane prompt as the `EFFORT` block)
```
EFFORT: medium (Pareto).
- First rank the changed files by risk: changed non-test lines, times 2 if the file matches one of these diff-shape signals: <lead pastes the signals.txt lines and the matching paths here>. Review the top files that hold about 80% of that score first. Review the rest only if time allows.
- Return every CRITICAL and HIGH finding. There is no cap on them.
- Return at most 5 MEDIUM, LOW or QUESTION findings: the ones with the highest impact × likelihood.
- Put the titles of any other findings you noticed in the OTHER line of the Lane result (titles only, no detail).
- adherence lane: evaluate every AC, task, B and P row, but return only the rows that are not Met, plus a count of the Met rows.
```

The lead fills in the signal line, because a lane that reads only bundle files may not open `signals.txt`. At `medium`, `ac-matrix.md` holds only the rows that the `adherence` lane returned, plus the count of Met rows.

### Verification and evaluation
- At most 2 blind verifiers (`standard` tier), batched by file via `scripts/merge_findings.py merge`. Verify only CRITICAL and HIGH findings, and `Missing` or `Contradicted` AC rows.
- Launch the Phase 2.3a `evaluation` subagent (`lanes/evaluation.md`, `standard` tier) **in parallel in the same message** as the blind verifiers on `nonblocking-candidates.json`.
- Run `scripts/merge_findings.py finalize <bundle_dir> --effort medium` to enforce the safety floor, run Carry G9 (if enabled), invoke `breaker.py`, and compute the deterministic Pareto cut (`<bundle_dir>/pareto.json`).

### Pareto cut (computed deterministically by `scripts/merge_findings.py finalize --effort medium`)
1. Score each merged finding: `score = severity weight × confidence weight × reach`.
   - Severity weight: CRITICAL 8, HIGH 5, MEDIUM 3, QUESTION 2, LOW 1.
   - Confidence weight: HIGH 1.0, MEDIUM 0.7, LOW 0.4.
   - Reach: 1.5 when the finding touches a main user path, a shared component or library, a public contract, a migration or stored data. Otherwise 1.0.
2. Sort by score, highest first. Break ties with the persona's `emphasis` and §1 order ("What they consistently flag").
3. Keep the smallest top set whose scores add up to at least 80% of the total score. Always keep every verified CRITICAL and HIGH, even past this point.
4. Report at most 10 findings and 3 questions from the kept set, in addition to all CRITICAL and HIGH.
5. List the rest, and the lanes' OTHER titles, in one line: "Other items that this review did not examine in depth: <a>, <b>, <c>." Keep this line in chat only. Do not post it.

### Quality gate
Compute it as at `high` on the lanes that ran. If security or performance was folded into conventions, add `(partial)` after the band, and never show a plain `PASS`.
