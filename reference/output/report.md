# Report (Phase 4)

## Report (Phase 4)

Print this to chat at `level=PR` (default). At `level=CODEBASE`, use the codebase report template in `reference/codebase.md` §Phase 3 & 4. Omit any section that is empty, except Context. Write all free text in STE. The headings below are the fixed STE headings.

```markdown
## <🔴|🟡|🟢> <verdict phrase from §Simplified Technical English> — <owner>/<repo>#<N> · <PR title>
<verdict_reason (one concise STE sentence)>
<when verdict_overrode_blocking is true:> **Override note:** <verdict_override_reason>.
**Top items:** <the 3 highest `post` findings as `ID title — path:line`, or "none">. **Counts:** <c> critical · <h> high · <m> medium · <l> low · <q> questions · <chat> chat-only

**Context:** head `<sha7>` · <draft · >spec `<path>` @ `<sha7>` (<named | changed in this PR | discovered in repo | issue as spec>) · tickets <KEY ✓ | KEY ✗ not readable: reason> · PR description <ok|one-line|empty>.
**Gaps:** <list, or "none">.   **Already raised:** <n> (Gate G5)   **Earlier findings (--since-last):** <n resolved · n still open · n changed, or omit>   **Repeats a decline:** <n, or omit>
**High risk:** <reasons, or omit when empty>.
**Review safety:** <n> findings of reviewer-directed text in unchanged files: `path:line` list. Chat only. Do not post or fold. Omit this line when none exist.
**Earlier declines still present:** <id · severity · path:line · decline reason, one line each>. Chat only. Do not post. Omit this line when none exist.

### ⚠️ Changes that break consumers
**H2 · <title>** — `path:line`
- Breaks: <consumers>
- Migration: <what they must do>

### Adherence
| Ref | Requirement | Status | Evidence | Test |
|---|---|---|---|---|
| AC1 | <≤15-word paraphrase> | 🟢 Met | `path:line` | `test_path:line` |
| AC2 | ... | 🟡 Partial — <note> | `path:line` | untested |
| AC3 | ... | 🔴 Missing | — | — |
| B2  | ... | 🔴 Contradicted — <note> | `path:line` | `test:line` |
| AC4 | ... | ⚪ Not verifiable — <note> | — | — |

**Out of scope or not expected:** <bullets: path — what changed — why it does not trace to the intent>
**PR claims that the evidence does not support:** <bullets, if any>

### Must fix (CRITICAL, HIGH)
**C1 · <title>** — `path:line` <add `` `plausible` `` or `` `unverified` `` after the location when the finding is not `verified: confirmed`>
<comment in STE: problem, evidence, impact, then **Fix:** as commands.>
<code_example as a ```suggestion block when it is a drop-in replacement, else a normal fenced block.>

### Fix before merge or soon after (MEDIUM)
**M1 · <title>** — `path:line`
<comment in STE.>

Not posted (chat only):
- **M9 · <title>** — `path:line` — <one STE sentence.>

### Does not block merge (LOW)
**L1 · <title>** — `path:line` — <one STE sentence.>
Minor items that do not block merge: <folded items with path:line>.
Other items that this review did not examine in depth: <medium Pareto cut and OTHER titles, chat only, omit when empty>.

### Questions for the author
1. **Q1** (`path:line`) — <1 STE sentence explaining the item>. <Direct question?>

### Earlier findings
- <kept / carried_open / carried_other: id · severity · path:line · status>
- Decline <id> challenged: <evidence>
- Earlier finding <id> cleared: <evidence>

---
Effort: <high|medium|low|skim> · Reach: <wide|related|callers|diff> · Verdict: <APPROVE|COMMENT|REQUEST_CHANGES> · <medium only: Pareto: <shown> of <total_post> post findings shown ·> Lanes: <lane ✓ | lane NOT ASSESSED (reason) | lane skipped (reason) | lane folded into conventions> · Quality gate: <PASS|CONCERNS|FAIL, with "(partial)" when a lane was folded|n/a> · Findings: <raw> raw → <merged> merged → <evaluated_post> post (<chat_only> chat-only) · Gates removed: <g2> out-of-diff, <g3> downgraded, <g4> low-confidence, <g5> already raised, <g6> linter, <refuted> refuted, <eval> evaluated-out · Persona: <refresh date>, approved | <refresh date>, draft | neutral (none) · Language: ASD-STE100, G8 clean
```

Emoji: 🔴 is used for `REQUEST_CHANGES` (`NEEDS ACTION` at `CODEBASE`) and for `Missing` or `Contradicted`, 🟡 for `COMMENT` (`IMPROVEMENTS RECOMMENDED` at `CODEBASE`) and `Partial`, 🟢 for `APPROVE` (`HEALTHY` at `CODEBASE`) and `Met`, and ⚪ for `Not verifiable`.

**Matrix size.** At every size:
- In chat, show only the rows that are not `Met`, plus one count line: `<n> Met — full matrix: <durable path>/ac-matrix.md`. Show every row only when the matrix has 5 rows or fewer.
- Write the full matrix to `<bundle_dir>/ac-matrix.md` when an AC matrix exists.
- Also write the full rendered report to `<bundle_dir>/report.md`. `scripts/merge_findings.py finalize` already writes `<bundle_dir>/findings.json` (all carried and current statuses) and `<bundle_dir>/reported.json` (kept `todo` findings); never overwrite `<bundle_dir>/findings.json` with a stripped subset.

**Durable copy (always).** `bundle_dir` is scratch space, and a background job deletes it. After you print the report, copy `report.md`, `ac-matrix.md` (when present), `findings.json`, `breaker-input.json`, and `verdict-decision.json` (when present at `level=PR`) to `~/.claude/review/reviews/<owner>-<repo>-<N|codebase>-<sha7>/`, and cite that durable path for the full AC matrix above. `findings.json` holds `{"pr": "<owner>/<repo>#<N|codebase>", "last_sha": "<head_sha>", "next_id": {...}, "findings": [...], "cleared": [...], "previous": [...], "reconcile": {...}}`. Subsequent reviews read it automatically (`carry/rereview.md`). Put no personal names, `@handles`, emails, or comment text from other people in it.
