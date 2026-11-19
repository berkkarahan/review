# Effort levels

`--effort=high|medium|low|skim|auto` (or natural-language effort intent) sets how much work the review does. When no explicit effort level is given, `/review` uses `auto` to choose the right depth from the PR facts (`effort/levels.md` §--effort=auto), while `high` defines the full baseline workflow described in `SKILL.md` and the reference files. `medium`, `low` and `skim` change the steps listed below, and every step not listed stays as it is at `high`.

`skim` is different in kind from the other three. `high`, `medium` and `low` are one workflow at three depths. `skim` is one subagent that has no inherited best practices, and it exists to reach a verdict as fast as possible on small PRs: version upgrades, and changes of fewer than 100 lines that have no large spec document.

Five rules hold at every level, `skim` included:
- **Safety floor.** No cap, cut or fold may drop a CRITICAL or HIGH finding. A lane that has more CRITICAL or HIGH findings than its cap returns all of them. The Pareto cut keeps every verified CRITICAL and HIGH.
- **STE.** Every text that a person reads is still in ASD-STE100 (`output/ste.md` §Simplified Technical English).
- **Gates.** G0 (PR state), G1–G4 (`scripts/diff_map.py` and `scripts/schema_lint.py`, which drops out-of-diff items under strict PR-diff scope), G8 (`scripts/ste_lint.py` for STE and question refreshers), and G10 (Phase 2.3a evaluation, subagent at `high`/`medium` and inline at `low`/`skim`) execute at every level. Neither Gate G10 nor any other gate may drop or lower a safety-floor CRITICAL or HIGH.
- **Verdict before cuts.** Compute the verdict, the event and the quality gate from the full merged set, before any Pareto cut or report reduction. A cut changes only what the report shows, never the verdict.
- **Breaker.** `scripts/breaker.py` runs at all four levels. It sets the verdict, the event and the quality gate. Run it with `--effort <level>`. At `low` and `skim` the band is `n/a`. At `skim` also pass `--in-profile` (when the PR is in profile) and `--ci <value>`. At `skim`, an `INVALID` result from the breaker is a skim failure (`effort/skim.md` §Skim verification). When an earlier `findings.json` exists and `--fresh` is not set (`gates.md` G9), `carry_findings.py prepare` and `reconcile` also run at every level. They cost no tokens. At `low` and `skim` the lead verifies inline, so it sets `verified: confirmed` on each CRITICAL or HIGH that it confirms. Without it, the safety floor in G9 cannot fire.

## Summary

| Step | high (baseline) | medium | low | skim |
|---|---|---|---|---|
| Bundle | full (`bundle.md` §2) | full, but context files capped at 10 files / 1.5k LOC | reduced diff only, no `head/` copy, no context files (`effort/low.md` §Low context reduction) | `diff.skim.patch` and `lock.patch`, no `head/` copy, no context files, no `signals.txt` (`effort/skim.md` §Skim context) |
| Intent pack | full | full | compact: ACs/promises, must-not-change statements, PR claims, conflicts | PR title, description and commit headlines only. No spec, no ticket (`effort/skim.md` §Skim context) |
| Inherited best practices | `lanes/checklist-<lane>.md`, repo rule files and convention docs, persona §8 & §9 | same, fewer lanes | the focus list inside the `critical-risk` method | none (`effort/skim-agent.md` §Skim agent) |
| Phase 1 lanes | cap 6 (`lanes/selection.md` §Selection; micro-lane auto-fold ≤40 lines / ≤2 files; `standard` tier) | cap 3 (`standard` tier) | lead inline if reduced diff < 400 lines, else 1 `critical-risk` lane (`fast` tier unless `--no-tier`) | exactly 1 `skim` subagent (`fast` tier unless `--no-tier`) |
| What lanes look for | all severities | all severities, Pareto order, at most 5 non-blocking findings per lane | CRITICAL and HIGH only | CRITICAL and HIGH, plus at most 3 QUESTION findings |
| AC matrix | all rows | all rows evaluated, only non-`Met` rows returned | only `Missing` and `Contradicted` rows, no test column | none |
| Verification | ≤ 6 verifiers (`standard` tier, overlapped with Evaluation) | ≤ 2 verifiers (`standard` tier, overlapped with Evaluation) | lead verifies inline, no verifier subagents | lead verifies inline, no verifier subagents |
| Evaluation (Phase 2.3a, G10) | 1 `evaluation` subagent (`standard` tier, parallel with verifiers on non-blocking + inline follow-up) | 1 `evaluation` subagent (`standard` tier, parallel with verifiers) | lead evaluates inline (`lanes/evaluation.md` rubric) | lead evaluates inline (`lanes/evaluation.md` rubric) |
| Quality gate | full | computed on the lanes that ran, marked `partial` when a lane was folded | `n/a` | `n/a` |
| Persona pass | full | full on the Pareto set | STE rewrite only, no re-rank or fold (there is nothing below HIGH) | STE rewrite only, no re-rank or fold |
| Gates (`gates.md`) | G0–G8, G10 | G0–G8, G10 | G0, G1–G4 by script, G5 if < 30 comments, G7 n/a, G8, G10 inline | G0, G1–G4 by script, G8, G10 inline |
| Per-lane cap | 8 non-blocking, titles of the rest in `OTHER` | 5 non-blocking, Pareto | n/a | n/a |
| Report | full (`output/report.md` §Report) | Pareto set + one "Other items" line | short form (`effort/low.md` §Low report) | short form (`effort/skim.md` §Skim report) |
| APPROVE event | allowed | allowed | never; the event is always `COMMENT` | allowed with confirmation, only when the PR is in profile, CI is `passing` and `!author_is_me` |

## Option precedence

Apply options in this order (whether passed as flags or picked up from the user's request):
0. `level=PR|CODEBASE` (`--level=PR|CODEBASE`) selects review scope. **Default is always `PR` unless explicitly set.** At `CODEBASE` (`reference/codebase.md`), `--effort=auto` resolves to `high` (exhaustive across all 7 lanes, no micro-folding, auto-sharded for large repos), `--effort=skim` is rejected, and `--post` / `--inline` / `--since-last` do not apply.
1. `--effort` selects the profile (`high`, `medium`, `low`, `skim` or `auto`). When omitted, resolve with `auto` below (or `high` when `level=CODEBASE`).
2. `--lanes=a,b` sets the exact lane set at `high` and `medium`, and replaces the effort lane cap. At `low`, `--lanes` narrows the focus list of the single `critical-risk` lane (or of the inline pass) to those lanes' concerns. Effort still sets the verification, the Pareto cut and the report form. At `skim`, ignore `--lanes` and add this line to the plan: "`--lanes` has no effect at skim."
3. `--no-inherit` runs `adherence` and `conventions` only. At `medium` this is 2 lanes. At `low` it removes the security and performance items from the `critical-risk` focus list. At `skim`, ignore it, because `skim` already inherits nothing. Add this line to the plan: "`--no-inherit` has no effect at skim."
4. `--inline` and small-PR mode apply at `high` and `medium` as described in `lanes/selection.md` §Selection, inside the effort lane cap. At `low` the size threshold below replaces them. At `skim`, ignore `--inline`, because `skim` always uses one subagent. Add this line to the plan: "`--inline` has no effect at skim."
5. `--spec` applies at `high`, `medium` and `low`. At `skim`, ignore it, because `skim` reads no spec. Add this line to the plan: "`--spec` has no effect at skim."
6. `--no-tier` disables `fast` model tiering on `low` and `skim` subagents, forcing `standard` (`inherit`). It has no effect at `high` and `medium`, which already run all subagents on `standard` (`inherit`).
7. `--post` works at every level. At `low` the event is always `COMMENT`, and `REQUEST_CHANGES` is offered only as an explicit option at posting time. At `skim` the event follows `effort/skim.md` §Skim verdict.

## `--effort=auto`

Pick the level from the facts when the user does not specify an explicit level:

| Condition, checked in order | Level |
|---|---|
| Bot author, or only lockfiles and manifests changed, or `skim` size profile holds (`effort/skim.md` §Skim size limit) | `skim` |
| Fewer than 400 core changed lines (`effort/low.md` §Low context reduction pathspec), no planned doc/spec, no `auth`, `data` or `contract` signal | `low` |
| Fewer than 1500 core changed lines and at most 2 planned docs/specs | `medium` |
| Otherwise | `high` |

Show the chosen level and the reason in the plan: `Effort: auto → <level> (<reason>)`. The user can re-run with an explicit level.
