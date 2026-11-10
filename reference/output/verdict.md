# Verdict (Phase 2.4–2.5)

**Source of the verdict.** The script `scripts/breaker.py` computes the verdict, the default event, the open counts, the AC count, the completeness and the quality-gate band, at every effort level. The lead runs the script and uses its output as it is. The lead must not compute these values again. The verdict rules (the condition table for each effort level, and the quality-gate banding) are in the docstring of `scripts/breaker.py`. The lead does not need to read them.

- **What counts.** The verdict counts verified CRITICAL and HIGH findings, and the MEDIUM and QUESTION findings that will be posted or shown under "Must fix", "Fix before merge or soon after" or "Questions for the author". It does not count chat-only items (an untested non-core AC, docs, PR description quality, formatting) or "pre-existing" items. A Pareto cut or Gate G7 changes only what the report shows, never the verdict (`effort/levels.md`). A review with only chat-only items gives APPROVE. A review with a QUESTION gives APPROVE WITH COMMENTS, because the reviewer cannot judge the PR until the author answers. The questions stay in the report.
- **Persona voice keys.** `voice.*` keys are read from the `persona.md` frontmatter. A missing key uses the default shown.
- **Holding a PR without Request Changes.** Some owners hold a PR with a COMMENT review and never use Request Changes. For them, `voice.hold_event: COMMENT`, and `REQUEST_CHANGES` is offered only as an explicit option at posting time.
- **Mechanical verdict.** The verdict itself stays mechanical, and the chat report states it plainly.
- **`author_is_me`.** The event is always `COMMENT`, because GitHub rejects approve and request-changes reviews on your own PR. The verdict is then framed as "ready / not ready for review".

**Run the breaker.** Run it at every effort level, after Phase 2.3 and before the Pareto cut:
```
python3 scripts/breaker.py <bundle_dir>/merged.json --effort <level> [--hold-event <event>] [--author-is-me] [--in-profile] [--ci <value>] [--pre <pre.json>] [--previous <findings.json>]
```
- **Input.** `merged.json` is `{"findings": [...], "ac_matrix": [...], "lanes": [...], "folded": [...]}`. Put these items in it:
  - the merged and verified findings, with chat-only items removed
  - the AC rows with their statuses after verification
  - one entry for each lane that ran: `status` is `returned`, `abstained` or `NOT ASSESSED`, and the last two need a `reason`
  - `folded`: the lanes that `medium` folded into `conventions`

  `--in-profile` and `--ci` apply only at `skim`. `--pre` is a file of the findings as the lanes returned them, with `verified` set. `--previous` is the `findings.json` of the earlier run.
- **Output.** The script prints JSON with `open` (a count for each severity), `unsatisfied`, `declined_blocking`, `complete`, `band`, `verdict`, `phrase`, `event`, `reasons` and `invalid`. Print `phrase` and the reason. It never changes the findings or any file.
- **Not counted.** The script does not count a finding that is `refuted`, a finding with `introduced_by_pr: pre-existing`, or a finding with `origin: existing`. A finding with no `status` is `todo`. A finding with no `origin` is `new`. Only `todo` findings are open.
- **Invalid input.** If the script exits with 1, it prints one `INVALID:` line for each problem. Correct only what each line names, and run the script again. Never change a severity, a status, an AC verdict or the set of findings to make the script pass. After a second failure, stop and show the lines to the user.
