# Effort: skim

## Skim

The goal is a verdict in minutes on a small PR: a version upgrade, or a change of fewer than 100 lines across at most 4 files that has no spec document or high-risk signal. One subagent reviews the diff with its own engineering judgement, and the lead verifies and writes. (`skim` is disallowed at `level=CODEBASE`.)

**No inherited best practices.** The skim agent uses none of these: the `lanes/checklist-<lane>.md`, repo convention docs, the target repo's rule files (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules/*`), the body of `persona.md` (including §8 house rules and §9 learned preferences), and specs or tickets. It uses the PR text, the diff and general engineering judgement only.

The five core rules in `effort/levels.md` still hold: safety floor, STE, gates, verdict before cuts, and breaker.

### Skim size limit

A PR is in profile for skim when all of these are true (`build_bundle.py` sets `gate_g0b.in_skim_profile`):
- `diff.skim.patch` has fewer than 100 changed lines (adds plus dels). Pure lockfiles and generated/locale files do not count; package manifests stay in `diff.skim.patch`.
- At most 4 files changed in `diff.patch`.
- The diff changes no spec or planned document (`intent.md` §3.3, step 2), and `--spec` is not set (`intent.md` §3.1).
- `signals.txt` has none of the `auth`, `data`, or `contract` signals.

If one of these is false, the PR is over the skim size. Skim still runs. The lead adds this line to the plan and to the report: `"This PR is over the skim size (<reason>). Run with --effort=low for a deeper review."` Over size is advice only. It does not change the verdict or the event.

### Skim context (Phase 0)

- **Persona.** Read only the `voice.*` keys in the `persona file (`persona_path`) frontmatter. Skip the body and the status and age warnings. The footer says `Persona: not applied (skim)`.
- **Clone.** Find the target clone with `bundle.md` §1. With a clone, fetch the PR head as in §2 (`git -C <clone> fetch -q origin "refs/pull/<N>/head"` and `git -C <clone> cat-file -e <head_sha>`), and set `<merge_base>` to `git -C <clone> merge-base <baseRefOid> <head_sha>`. With no clone, use the API fallbacks in §2.
- **PR metadata.**
  ```
  gh pr view <url> --json number,title,body,author,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,additions,deletions,changedFiles,commits,labels,statusCheckRollup,url > pr.json
  gh pr diff <url> > diff.patch
  ```
  Write `files.txt` as in §2 with a clone, and run `scripts/diff_map.py diff.patch > diff-map.json` for Gate G2. With no clone, derive `files.txt` from the `diff --git` headers and the `+`/`-` line counts of `diff.patch`, because `gh pr view --json files` returns at most 100 files. Do not read `diff.patch` into the lead's context, because only `diff_map.py` and Posting step 2 use it.
- **CI.** Compute one CI value from `statusCheckRollup` in `pr.json`:
  - `failing: <check names>` if any entry has conclusion `FAILURE`, `TIMED_OUT`, `CANCELLED`, `ACTION_REQUIRED`, or `STARTUP_FAILURE`, or state `ERROR` or `FAILURE`
  - `pending` if any entry is not complete
  - `none` if the list is empty
  - `passing` in all other cases
- **Diff without lockfiles.** Build `diff.skim.patch` from the merge base (keeping tests and package manifests, excluding pure lockfiles and generated/locale files):
  ```
  git -C <clone> diff -U3 <merge_base> <head_sha> -- . \
    ':(exclude,glob)**/*.lock' ':(exclude,glob)**/package-lock.json' ':(exclude,glob)**/pnpm-lock.yaml' ':(exclude,glob)**/yarn.lock' \
    ':(exclude,glob)**/poetry.lock' ':(exclude,glob)**/Pipfile.lock' ':(exclude,glob)**/Cargo.lock' ':(exclude,glob)**/Gemfile.lock' \
    ':(exclude,glob)**/composer.lock' ':(exclude,glob)**/go.sum' ':(exclude,glob)**/uv.lock' ':(exclude,glob)**/bun.lockb' ':(exclude,glob)**/bun.lock' \
    ':(exclude,glob)**/__snapshots__/**' ':(exclude,glob)**/*.snap' ':(exclude,glob)**/locale/**' ':(exclude,glob)**/locales/**' \
    ':(exclude,glob)**/*.po' ':(exclude,glob)**/*.min.js' ':(exclude,glob)**/generated/**'
  ```
  Build `lock.patch` from pure lockfiles only, with 3 lines of context, so that the version line stays next to a changed hash:
  ```
  git -C <clone> diff -U3 <merge_base> <head_sha> -- ':(glob)**/*.lock' ':(glob)**/package-lock.json' ':(glob)**/pnpm-lock.yaml' ':(glob)**/yarn.lock' ':(glob)**/poetry.lock' ':(glob)**/Pipfile.lock' ':(glob)**/Cargo.lock' ':(glob)**/Gemfile.lock' ':(glob)**/composer.lock' ':(glob)**/go.sum' ':(glob)**/uv.lock' ':(glob)**/bun.lockb' ':(glob)**/bun.lock'
  ```
  With no clone, split `diff.patch` at its `diff --git` headers into the same two files, and record the gap. Count the size from `diff.skim.patch`. Use anchored patterns. A loose pattern such as `*lock*` also matches `block` and `clock`.
- **No `head/` copy, no `context_files.txt`.** (`build_bundle.py` still writes `signals.txt` to check `in_skim_profile`, though the skim agent does not read it.) The agent reads a line range only when a hunk needs it.
- **Intent.** Write `intent.md` with only: Sources (`PR description: yes | one-line | empty`), the PR claims (P1..Pn, from the title, the description and the commit headlines), and Gaps. Gaps always says `"Specs and tickets not read (skim)"`. If the PR text has a ticket key or a spec path, list it there as `"referenced, not read"`. Do not call a ticket tracker and do not read a spec document.
- **No question to the user.** Skip the AskUserQuestion exception for a thin description. Proceed, and let the report show the gap.
- **No tasks.** Do not create per-lane tasks.
- **Plan.** Show this instead of the four-line plan:
  ```
  Review plan — <owner>/<repo>#<N> @ <short sha> (<+adds/-dels>, <files> files) · Effort: skim
  Size: <n> changed lines · <in profile | over the skim size: <reason>> · CI: <value>   Gaps: <list or none>
  Lane (1): skim — one agent, no inherited best practices.
  ```
  Add the flag lines from `effort/levels.md` §Option precedence when they apply.

### Skim verification and evaluation (Phase 2)

- Save the `skim` agent's JSON result to `<bundle_dir>/raw-lanes.json` and run `python3 <skill_dir>/scripts/merge_findings.py merge <bundle_dir>` (runs Gates G1–G5 and drops out-of-diff items). Retry `skim` once on `retry_lanes` (G1 failure). After a second failure, print `"Skim failed: <reason>. This review has no verdict."` and stop without computing a verdict or offering to post.
- Do not add a finding for CI status, and do not report other obvious facts that the author and the reviewer already see (a failed check, a red status, the PR size). CI appears only in the Context line of the chat report. It has no effect on the verdict or the event.
- Verify each CRITICAL and HIGH finding inline, as at `low`: re-read the cited range at `head_sha` and one caller or guard, writing `<bundle_dir>/verifier-results.json` (`"refuted"` if evidence fails, `"unclear"` to downgrade to a question, `"plausible"` if the path exists and nothing refutes it but the trigger depends on input, config or state that the code cannot show, `"confirmed"` if verified). Write a verdict for every CRITICAL and HIGH. A finding with no verdict becomes `verified: unverified`.
- Apply the Phase 2.2 `evaluation` rubric (`lanes/evaluation.md`) inline to surviving candidates (confirming every surviving item belongs on the PR via `post` | `chat-only` | `drop` while keeping all safety-floor items at `post`) into `<bundle_dir>/eval-results.json`, then run `python3 <skill_dir>/scripts/merge_findings.py finalize <bundle_dir> [--carry] [--still-open <file>] [--cleared <file>]`. If `finalize` exits 1 (`INVALID`), treat it as a skim failure and stop.
- There is no quality gate, no AC matrix and no Pareto cut.

### Skim verdict

The verdict, the phrase and the event come from `breaker.py` (called by `merge_findings.py finalize`) and follow `output/verdict.md`, as at every effort level. `skim` has no special table. The PR size, the CI value and `in_profile` do not change the verdict or the event. A posted `APPROVE` needs the explicit confirmation in `output/posting.md` §Posting step 5, as at every level.

### Skim report

Print this short form instead of `output/report.md` §Report. Write it in STE. Omit the "Must fix" and "Questions" sections if they are empty.
```markdown
## <🔴|🟡|🟢> <verdict phrase> — <owner>/<repo>#<N> · <PR title>
<one-sentence reason>

**Change:** <one STE sentence: what the diff does, for example "This PR changes `foo` from 1.2.3 to 1.2.9 in `package.json`.">
**Context:** head `<sha7>` · CI <passing | failing: <names> | pending | none> · size <n> changed lines (+ <m> lockfile lines) · PR description <ok|one-line|empty>
**Gaps:** <list, or "none">. This is a skim review. It did not read specs or tickets. Its reach is the diff lines only.
<if over size> **Warning:** This PR is over the skim size (<reason>). Run with `--effort=low` for a deeper review.
<include **High risk:** / **Review safety:** / **Earlier declines still present:** when non-empty>

### Must fix (CRITICAL, HIGH)
<findings, same format as §Report>

### Questions for the author
1. **Q1** (`path:line`) — <1 concise STE sentence explaining the item>. <Direct question?>   # at most 3

---
Effort: skim · Reach: diff · Lanes: skim ✓ · Quality gate: n/a · Findings: <raw> raw → <kept> post · Gates removed: <g2> out-of-diff, <g5> already raised, <refuted> refuted, <eval> evaluated-out · Persona: not applied (skim) · Language: ASD-STE100, G8 clean
```

Phase 3 at skim is the STE rewrite and the self-check only.

**Posted body at skim.** Post one short bullet list and nothing else, so that the author reads it in seconds:
- Write 2 to 5 bullets. Each bullet is one STE sentence or two short ones, and it sums up one point: what the diff does when the claim holds, and each finding or question with its fix or ask.
- Merge related findings and questions into one bullet. Do not use headings, a verdict line, a Context line, a Gaps line or a footer.
- Do not put CI status or other obvious facts in a bullet.
- Put no inline comments in the payload, unless a CRITICAL or HIGH finding needs a line anchor.
- The last line is the STE acknowledgement (`output/ste.md` §Simplified Technical English).
- The chat report keeps the full short form (§Skim report). Only the posted body is reduced.
