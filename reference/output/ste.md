# Simplified Technical English

## Simplified Technical English (ASD-STE100)

Every text that a person reads is written in ASD-STE100 Simplified Technical English (STE), Issue 9 (January 2025): https://www.asd-ste100.org/. The full standard is a free PDF, available on request from that site.

**Scope.** STE applies to:
- the Phase 0 review plan
- the Phase 4 chat report: headings, verdict line, Context, Scope, Gaps, AC-matrix notes, finding text, **Fix:** and **Ask:** lines, and the footer
- the posted review body and every inline comment
- questions to the author, and the AskUserQuestion text that the user sees

STE also takes priority over a session output style that removes words to make text shorter (for example, a terse mode that drops articles). STE does not permit you to omit words to make a sentence shorter.

STE does not apply to lane prompts, verifier prompts, `intent.md` or other internal files. Lanes stay persona-neutral and style-neutral, and Phase 3 owns the words.

**Verbatim items.** Do not rewrite these, and count each one as one word:
- code spans, file paths, `path:line`, identifiers, commands and URLs
- FR, NFR, AC, B, D, S, P, A, R, V, K, X, Y, T, OQ, AD, PD, C, H, M, L and Q IDs, ticket keys, PR and issue references, and SHAs
- quoted spec, ticket, requirement or code-comment text (keep it inside quotation marks)
- the `voice.comment_prefix` and `voice.nit_prefix` values (for example `[HIGH / HIGH]`, `[HIGH]`, `Nit:`, and `NIT:`), `suggestion` blocks and fenced code
- the enum values `Met`, `Partial`, `Missing`, `Contradicted` and `Not verifiable`, the verdict names, the GitHub event names and the lane names
- @-mentions and mention placeholders
- technical nouns and technical verbs of the subject field (for example `publish`, `serializer`, `codegen`, `impersonation`, `pending`). STE permits them.

**Rules that the STE website states:**
1. Use approved words, each with its approved meaning and part of speech only. Each approved word has one meaning. Examples from the site:
   - use *start*, not *begin*, *commence* or *initiate*
   - use *do*, not *achieve*, *carry out* or *accomplish*
   - *about* means only "concerned with", not "approximately"
   - *check* is a noun only ("do a check"), not a verb (note: `ste_lint.py` allows `check` in instruction lists for compatibility)

   If you are not sure that a general word is approved, use the simplest common word that has one clear meaning.
2. Do not use "-ing" forms. Exceptions: technical nouns (for example `pending`, `string`), and dictionary words that are not verbs (*during*, *something*, *opening*, *remaining*). This rule also applies to headings.
3. Write each instruction as a command, with the imperative form of the verb. This applies to each **Fix:** line and to the fix part of each comment. Write "Add FRM-BE-PUBLISH to the dependencies.", not "FRM-BE-PUBLISH should be added." or "Can we add FRM-BE-PUBLISH?".
4. Use the active voice in descriptive text (the issue and the impact). Use the passive voice only when you do not know the agent.
5. If a sentence has a condition, put the condition first: "If the flag is off, the route returns 404."
6. Write short sentences, with one topic in each sentence.
7. Use American English spelling (Merriam-Webster), whatever `voice.spelling` says.

**Rules from earlier issues of the standard (confirm them against the Issue 9 PDF):**
8. An instruction has a maximum of 20 words. A descriptive sentence has a maximum of 25 words.
9. A paragraph has a maximum of 6 sentences. For more items or steps, use a vertical list.
10. A noun cluster has a maximum of 3 words. Use a technical noun, or write the words again with a preposition ("the endpoint that counts the templates in each bucket").
11. Use only these verb forms: the imperative, the infinitive, the simple present, the simple past and the future. Use the past participle only as an adjective, or with a form of "to be" in the passive voice.
12. Do not omit articles or "that" to make a sentence shorter.
13. Write one instruction in each sentence, unless the two actions occur at the same time.

**Skill conventions that follow from STE.** These rules are for this skill, not rules of the standard:
- Use a colon to introduce a list, not a semicolon.
- Do not use chat abbreviations (for example "wdyt", "IMO", "FYI").
- Do not use hedges ("I think", "Maybe", "How about", "I am not sure but"). The `[SEVERITY / CONFIDENCE]` prefix shows the confidence. If necessary, add one sentence: "This is not verified."
- Give each finding this shape: the problem (1–2 descriptive sentences), the evidence (`path:line`), the impact (1 sentence), and the fix (1–3 commands). For more than 3 commands, use a vertical list.
- **Question & ID Refresher Rule:** For every author question (`QUESTION` findings / `Questions for the author` / `Open questions`), include **1 short STE sentence** (≤ 25 words) explaining what the item is before the direct question (`**Q1** (\`path:line\`) — <1-sentence refresher>. <Direct question?>`). Whenever asking the human-in-the-loop (`AskUserQuestion` prompts, option labels, per-finding post/drop/fix/decline/memory prompts) about an item ID (`C1`, `H2`, `Q1`, `B1`, `S2`, `X1`, `Y2`, `AC3`, `P1`, etc.), never cite a bare ID alone: include **1 short STE sentence** (≤ 25 words) explaining that item before or alongside the question or option.

**Fixed headings and phrases** (these replace the persona labels in `output/report.md` §Report):

| Use | STE text |
|---|---|
| Verdict REQUEST CHANGES / APPROVE WITH COMMENTS / APPROVE | "Not ready to merge" / "Ready to merge after the comments" / "Ready to merge" |
| Verdict when `author_is_me` | "Not ready for review" / "Ready for review" |
| Verdict at `level=CODEBASE` (`NEEDS ACTION` / `IMPROVEMENTS RECOMMENDED` / `HEALTHY`) | "Codebase needs urgent fixes" / "Codebase needs targeted improvements" / "Codebase is in good shape" |
| Adherence section (`PR` / `CODEBASE`) | "Adherence" / "Spec and documentation drift" |
| CRITICAL and HIGH section | "Must fix (CRITICAL, HIGH)" |
| MEDIUM section (`PR` / `CODEBASE`) | "Fix before merge or soon after (MEDIUM)" / "Recommended improvements (MEDIUM)" |
| LOW section (`PR` / `CODEBASE`) | "Does not block merge (LOW)" / "Low-priority improvements (LOW)" |
| Folded LOW line (`PR` / `CODEBASE`) | "Minor items that do not block merge: <a>, <b>, <c>." / "Minor improvements: <a>, <b>, <c>." |
| Medium Pareto remainder | "Other items that this review did not examine in depth: <a>, <b>, <c>." |
| Chat-only list (`PR` / `CODEBASE`) | "Not posted (chat only):" / "Additional notes (chat only):" |
| Questions section (`PR` / `CODEBASE`) | "Questions for the author" / "Open questions" |
| Breaking changes (`PR` / `CODEBASE`) | "⚠️ Changes that break consumers" / "⚠️ Contract and compatibility risks" |
| Out-of-scope bullets | "**Out of scope or not expected:**" |
| PR claims | "**PR claims that the evidence does not support:**" |
| STE acknowledgement (last line of the posted review body, one time only) | "This review uses ASD-STE100 Simplified Technical English (STE): https://www.asd-ste100.org/" |

**STE self-check.** Do this check before you print the report, and again before you write `review-payload.json`. First run Gate G8:
- On the report: `python3 scripts/ste_lint.py <bundle_dir>/report.md --findings <bundle_dir>/reported.json` (use `--findings <bundle_dir>/pareto.json` at `effort=medium`).
- On the posted review body and on each inline comment: `python3 scripts/ste_lint.py <file>` (omit `--findings`, or pass only the subset of findings rendered in that text).

Fix every line that the script prints. The script finds length, "-ing", semicolon, hedge, missing question-refresher, and lost-evidence problems. It cannot judge the rest, so then do the checklist below by hand. No tool replaces the standard (STE website FAQ). For each sentence, ignore the verbatim items, then confirm these points:
- [ ] Each instruction is a command, with a maximum of 20 words.
- [ ] Each descriptive sentence is in the active voice, with a maximum of 25 words.
- [ ] Each sentence has one topic. No paragraph has more than 6 sentences.
- [ ] There are no "-ing" verb forms, and no noun cluster has more than 3 words.
- [ ] No article is omitted. There are no semicolons and no chat abbreviations.
- [ ] Each condition comes before its result.
- [ ] Every author question and every ID-citing human-in-the-loop prompt includes 1 short STE sentence explaining that item.
- [ ] Each `path:line`, identifier, number and fix action from the verified finding is still in the text. STE changes the words, not the evidence.
