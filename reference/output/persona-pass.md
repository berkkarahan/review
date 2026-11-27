# Persona pass (Phase 3) and configuration

## Persona pass (Phase 3)

Input: evaluated findings (`eval_disposition: post` and `eval_disposition: chat-only`), plus the persona file (`persona_path` in `bundle-summary.json`). **Zero-finding fast path:** when `zero_findings_fast_path` is `true` (zero `post` and zero `chat-only` findings with verdict `APPROVE` at `level=PR` or `HEALTHY` at `level=CODEBASE`), skip this file, `persona.md` body, and `ste-examples.md` completely and render the concise STE report directly. Keep `chat-only` items strictly under `Not posted (chat only):` (`Additional notes (chat only):` at `level=CODEBASE`) in chat and never promote them to posted PR comments. If `persona.md` is missing or has `status: empty` and no learned preferences in §9, use the default neutral voice: write in STE (`output/ste.md` §Simplified Technical English), concise and direct, with no filler.

1. **Re-rank** within each severity by the persona's `emphasis` list, §1 ("What they consistently flag"), §6 ("Business and domain priorities"), and §9 ("Learned preferences" with type `emphasis` or `check`).
2. **Display folding only (never drop after `breaker.py`).** Phase 3 runs after `merge_findings.py finalize`, Phase 2.4 `verdict`, and `breaker.py`, so it never drops findings or reclassifies `post` findings as `chat-only` (suppression and `chat-only` routing happen earlier in Phase 1 `conventions` and Phase 2.2 Gate G10 `evaluation`, before counts and the verdict are finalized). For `LOW` findings matching §2 ("What they let slide"), Phase 3 may fold them visually onto one line while keeping each item's `path:line`:
   - At `level=PR`: `Minor items that do not block merge: <a> (<path:line>), <b> (<path:line>).`
   - At `level=CODEBASE`: `Minor improvements: <a> (<path:line>), <b> (<path:line>).`
   Never drop or fold a verified `CRITICAL` or `HIGH` finding.
3. **Severity labels.** Use the fixed STE section headings (`output/ste.md` §Simplified Technical English, "Fixed headings and phrases"). The persona's severity vocabulary (`persona.md` §3) selects the STE form from the phrase-mapping table. It does not supply non-STE words. Posted inline comment prefixes come from `voice.*` keys. The finding ID letter keeps the true severity (`C`/`H`/`M`/`L`/`Q` or stable carry ID).
4. **Tone of communication and voice.** ASD-STE100 grammar rules govern sentence construction, and the persona (`voice.tone`, §4, and §9 `tone` preferences) governs the communication style, structure, and emphasis:
   - `direct` (default): state the problem, evidence, impact, and imperative fix command directly.
   - `constructive`: pair each problem statement with a clear explanation of the benefit and, whenever possible, a concrete ```suggestion code block.
   - `mentor`: include one short STE sentence explaining the underlying architectural or domain principle before the fix command.
   - `strict`: emphasize contract boundaries, test requirements, and risk in the impact sentence.
   - `concise`: keep each comment to the minimum STE sentences needed (1 problem sentence with `path:line` evidence + 1 imperative fix command).
   - Two renderings exist for each finding:
     - **Chat report** (`output/report.md` / `codebase.md`): `**C1 · <title>** — `path:line``, followed by the STE problem, evidence, impact, and `**Fix:**` commands (`LOW` uses `**L1 · <title>** — `path:line`` with one STE sentence or the folded line; `QUESTION` uses `1. **Q1** (`path:line`) — <1 STE sentence explaining the item>. <Direct question?>`).
     - **Posted inline comment** (`level=PR` only, written in STE):
       - `CRITICAL`/`HIGH`/`MEDIUM` start with `voice.comment_prefix` (with `{SEVERITY}` and `{CONFIDENCE}` filled in when present).
       - `LOW` starts with `voice.nit_prefix` (default `Nit:`).
       - `QUESTION` has no prefix.
       - Emoji follow `voice.emoji` (default `none`). `sparse` means at most one, and only on `LOW` or `QUESTION` comments.
       - Keep within the persona's length norms and the STE sentence limits. Use a ```suggestion block for drop-in one-line fixes.

   The rewrite changes only presentation and structure:
   - Keep the technical claim, the evidence, and the fix intact. Each `path:line`, identifier, number, and fix action must remain in the text.
   - Do not add opinions, jokes, or phrases that `persona.md` does not support.
   - Gate G4 drops `MEDIUM`/`LOW` findings with `LOW` confidence, and `finalize` turns a verifier `unclear` into a `QUESTION` before Phase 3. A `plausible` or `unverified` `CRITICAL`/`HIGH` keeps its level and its tag in the report; for `QUESTION` or `MEDIUM`-confidence items, keep tone neutral and factual.
5. **Language and spelling.** Use American English (Merriam-Webster), because ASD-STE100 requires it. This applies regardless of `voice.spelling`. Verbatim items (`output/ste.md` §Simplified Technical English) remain unchanged.
6. **Opening and closing.** Use the fixed STE verdict phrases for the verdict line. Add no greeting and no sign-off. The only closing line is the STE acknowledgement at the end of the posted PR review body (`output/posting.md` §Posting step 3).
7. **Safety floor.** A verified `CRITICAL` or `HIGH` correctness, security, data-loss/corruption, unmigrated-breaking, or `ac_ref` finding cannot be dropped, folded, or softened below the blocking heading. It can only be reworded.
8. **Self-authored PR.** If `author_is_me`, write comments as notes to self ("Fix this before you request a review."). Keep the same technical content.
9. **Posted volume cap (Gate G7, `level=PR` only — skipped at `level=CODEBASE`).** Post **at most `voice.max_inline` inline comments** (default 8), at most 8 `MEDIUM` findings in posted PR comments, and at most 3 `LOW` findings from house rules. Set `voice.max_inline` from the persona's observed comments per PR, not from total finding count:
   - Rank by severity, then by the persona's `emphasis`, §1 order, and §8/§9 rules.
   - Move excess non-blocking items strictly to `Not posted (chat only):` in chat. Never dump excess items into an `"Also:"` grab-bag in the posted PR body.
   - Verified `CRITICAL` and `HIGH` findings always get an inline comment, or a body bullet if their line is not in the diff (`adherence` intent findings only), even past the cap.
   - The chat report still lists all `post` (subject to `pareto.json` at `medium`) and `chat-only` findings.
10. **STE self-check.** Run Gate G8 (`scripts/ste_lint.py` with `--findings <bundle_dir>/reported.json`, or `<bundle_dir>/pareto.json` at `medium`) and the manual STE checklist (`output/ste.md`) on the report before printing it.

## Interactive persona and preference configuration (via `/review`)

When the user invokes `/review` to configure or update their persona or preferences (or asks to set up their review persona):
1. Read the persona file (`persona_path` in `bundle-summary.json`). If `~/.claude/review/persona.md` does not exist, create `~/.claude/review/` and copy `<skill_dir>/reference/persona.md` there first.
2. Ask the user (or apply what they already stated in their prompt) for:
   - **Tone of communication (`voice.tone`)**: `direct`, `constructive`, `mentor`, `strict`, or `concise` (and optionally `voice.hold_event`, `voice.max_inline`, `voice.comment_prefix`, `voice.nit_prefix`, `voice.emoji`).
   - **PR emphasis (`emphasis` and §1/§6)**: which concerns matter most on PRs (for example `security`, `backward-compatibility`, `test-coverage`, `performance`, `domain-modeling`).
   - **What to let slide (§2)**: minor items to ignore in Phase 1/2.2 or fold in chat.
   - **House rules (§8)**: objective yes/no checks to run on every diff.
   - **Memory acquisition (`memory`)**: `enabled`, `ask`, or `disabled`.
3. Update `~/.claude/review/persona.md` (never anything under `<skill_dir>/reference/`) with `status: approved` (or `draft` if the user wants to test it first), update §7 Provenance, and set `refreshed` to today's `YYYY-MM-DD`. Write no personal names, emails, or `@handles`.
