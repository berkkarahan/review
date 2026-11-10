# Effort: skim

## Skim

The goal is a verdict in minutes on a small PR: a version upgrade, or a change of fewer than 100 lines that has no large spec document. One subagent reviews the diff with its own engineering judgement, and the lead verifies and writes.

**No inherited best practices.** The skim agent uses none of these: the `lanes/checklist-<lane>.md`, repo convention docs, the target repo's rule files (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules/*`), the body of `persona.md` (including §8 house rules and §9 learned preferences), and specs or tickets. It uses the PR text, the diff and general engineering judgement only.

The three rules at the top of this file still hold: the safety floor, STE, and "verdict before cuts".

### Skim size limit

A PR is in profile for skim when all of these are true:
- `diff.skim.patch` has fewer than 100 changed lines (adds plus dels). Lockfile and generated-file lines do not count.
- The diff changes no spec or planned document (`intent.md` §3.3, step 2).
- The PR title, description and branch name name no spec path, and `--spec` is not set (`intent.md` §3.1).

If one of these is false, the PR is over the skim size. Skim still runs. The lead adds this line to the plan and to the report: "This PR is over the skim size (<reason>). Run with `--effort=low` for a deeper review." An over-size PR never gets an `APPROVE` event.

### Skim context (Phase 0)

- **Persona.** Read only the `voice.*` keys in the `persona.md` frontmatter. Skip the body and the status and age warnings. The footer says `Persona: not applied (skim)`.
- **Clone.** Find the target clone with `bundle.md` §1. With a clone, fetch the PR head as in §2 (`git -C <clone> fetch -q origin "refs/pull/<N>/head"` and `git -C <clone> cat-file -e <head_sha>`), and set `<merge_base>` to `git -C <clone> merge-base <baseRefOid> <head_sha>`. With no clone, use the API fallbacks in §2.
- **PR metadata.**
  ```
  gh pr view <url> --json number,title,body,author,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,additions,deletions,changedFiles,commits,labels,statusCheckRollup,url > pr.json
  gh pr diff <url> > diff.patch
  ```
  Write `files.txt` as in §2 with a clone. With no clone, derive it from the `diff --git` headers and the `+`/`-` line counts of `diff.patch`, because `gh pr view --json files` returns at most 100 files. Do not read `diff.patch` into context, because only Posting step 2 uses it.
- **CI.** Compute one CI value from `statusCheckRollup` in `pr.json`:
  - `failing: <check names>` if any entry has conclusion `FAILURE`, `TIMED_OUT`, `CANCELLED` or `ACTION_REQUIRED`, or state `ERROR` or `FAILURE`
  - `pending` if any entry is not complete
  - `none` if the list is empty
  - `passing` in all other cases
- **Diff without lockfiles.** Build `diff.skim.patch` from the merge base. It keeps tests, because tests are cheap to read in a small PR:
  ```
  git -C <clone> diff -U3 <merge_base> <head_sha> -- . \
    ':(exclude,glob)**/*.lock' ':(exclude,glob)**/package-lock.json' ':(exclude,glob)**/pnpm-lock.yaml' ':(exclude,glob)**/go.sum' \
    ':(exclude,glob)**/__snapshots__/**' ':(exclude,glob)**/*.snap' ':(exclude,glob)**/locale/**' ':(exclude,glob)**/locales/**' \
    ':(exclude,glob)**/*.po' ':(exclude,glob)**/*.min.js' ':(exclude,glob)**/generated/**'
  ```
  Build `lock.patch` from the lockfiles only, with 3 lines of context, so that the version line stays next to a changed hash:
  ```
  git -C <clone> diff -U3 <merge_base> <head_sha> -- ':(glob)**/*.lock' ':(glob)**/package-lock.json' ':(glob)**/pnpm-lock.yaml' ':(glob)**/go.sum'
  ```
  With no clone, split `diff.patch` at its `diff --git` headers into the same two files, and record the gap. Count the size from `diff.skim.patch` (`git diff --numstat` with the same pathspec, and sum adds and dels). Use anchored patterns. A loose pattern such as `*lock*` also matches `block` and `clock`.
- **No `head/` copy, no `context_files.txt`, no `signals.txt`.** The agent reads a line range only when a hunk needs it.
- **Intent.** Write `intent.md` with only: Sources (`PR description: yes | one-line | empty`), the PR claims (P1..Pn, from the title, the description and the commit headlines), and Gaps. Gaps always says "Specs and tickets not read (skim)". If the PR text has a ticket key or a spec path, list it there as "referenced, not read". Do not call a ticket tracker and do not read a spec document.
- **No question to the user.** Skip the AskUserQuestion exception for a thin description. Proceed, and let the report show the gap.
- **No tasks.** Do not create per-lane tasks.
- **Plan.** Show this instead of the four-line plan:
  ```
  Review plan — <owner>/<repo>#<N> @ <short sha> (<+adds/-dels>, <files> files) · Effort: skim
  Size: <n> changed lines · <in profile | over the skim size: <reason>> · CI: <value>   Gaps: <list or none>
  Lane (1): skim — one agent, no inherited best practices.
  ```
  Add the flag lines from `effort/levels.md` §Option precedence when they apply.

### Skim verification (Phase 2)

- Validate the result as in Phase 2.1. Retry once. After a second failure, print "Skim failed: <reason>. This review has no verdict." and stop. Do not compute a verdict and do not offer a post.
- Drop any MEDIUM or LOW finding that the agent returns, and count it in "dropped".
- Do not add a finding for CI status, and do not report other obvious facts that the author and the reviewer already see (a failed check, a red status, the PR size). CI appears only in the Context line of the chat report, and it still blocks `APPROVE` (§Skim verdict).
- The lead verifies each CRITICAL and HIGH finding inline, as at `low`: re-read the cited range at `head_sha` and one caller or guard. Drop a finding that the evidence does not support. Downgrade an unclear finding to a question.
- There is no quality gate, no AC matrix and no Pareto cut.

### Skim verdict

Compute the verdict and the default event from the verified findings, before any rewrite. `scripts/breaker.py --effort skim` reproduces this table. Use its output.

| Condition, checked in order | Verdict phrase | Default event |
|---|---|---|
| Any verified CRITICAL or HIGH | 🔴 "Not ready to merge" | `voice.hold_event` if any finding is CRITICAL, else `COMMENT` |
| Any QUESTION | 🟡 "Ready to merge after the comments" | `COMMENT` |
| Nothing | 🟢 "Ready to merge" | `APPROVE` if the PR is in profile, `!author_is_me` and CI is `passing`, else `COMMENT` |

- If `author_is_me`, use "Not ready for review" and "Ready for review" as the phrases, and the event is always `COMMENT`.
- An over-size PR gets `COMMENT` in every row. A PR with CI `pending` or `none` never gets `APPROVE`, because CI is the main evidence for a version upgrade.
- The reason line of a 🟢 or 🟡 verdict ends with: "This review examined only CRITICAL and HIGH issues."
- An `APPROVE` payload needs the explicit confirmation in `output/posting.md` §Posting step 5, as at every level.

### Skim report

Print this short form instead of `output/report.md` §Report. Write it in STE. Omit the "Must fix" and "Questions" sections if they are empty.
```markdown
## <🔴|🟡|🟢> <verdict phrase> — <owner>/<repo>#<N> · <PR title>
<one-sentence reason>

**Change:** <one STE sentence: what the diff does, for example "This PR changes `foo` from 1.2.3 to 1.2.9 in `package.json`.">
**Context:** head `<sha7>` · CI <passing | failing: <names> | pending | none> · size <n> changed lines (+ <m> lockfile lines) · PR description <ok|one-line|empty>
**Gaps:** <list, or "none">. This is a skim review. It did not read specs or tickets. It examined only CRITICAL and HIGH issues.
<if over size> **Warning:** This PR is over the skim size (<reason>). Run with `--effort=low` for a deeper review.

### Must fix (CRITICAL, HIGH)
<findings, same format as §Report>

### Questions for the author
<at most 3>

---
Effort: skim · Lanes: skim ✓ · Quality gate: n/a · Findings: <raw> raw → <kept> kept → <dropped> dropped by the lead · Persona: not applied (skim) · Language: ASD-STE100, self-check done
```

Phase 3 at skim is the STE rewrite and the self-check only.

**Posted body at skim.** Post one short bullet list and nothing else, so that the author reads it in seconds:
- Write 2 to 5 bullets. Each bullet is one STE sentence or two short ones, and it sums up one point: what the diff does when the claim holds, and each finding or question with its fix or ask.
- Merge related findings and questions into one bullet. Do not use headings, a verdict line, a Context line, a Gaps line or a footer.
- Do not put CI status or other obvious facts in a bullet.
- Put no inline comments in the payload, unless a CRITICAL or HIGH finding needs a line anchor.
- The last line is the STE acknowledgement (`output/ste.md` §Simplified Technical English).
- The chat report keeps the full short form (§Skim report). Only the posted body is reduced.

## Footer

At every level, add `Effort: <level>` as the first item of the report footer. At `medium`, also add `Pareto: <shown> of <merged> findings shown`.
