# Persona pass (Phase 3) and configuration

## Persona pass (Phase 3)

Input: evaluated findings (`eval_disposition: post` and `eval_disposition: chat-only`), plus `reference/persona.md`. **Zero-finding fast path:** when `zero_findings_fast_path` is `true` (zero `post` and zero `chat-only` findings with verdict `APPROVE`), skip this file, `persona.md` body, and `ste-examples.md` completely and render the concise STE approve report directly. Keep `chat-only` items strictly under `Not posted (chat only):` in the chat report and never promote them to posted PR comments. If `persona.md` is missing or has `status: empty` and no learned preferences in §9, use the default neutral voice: write in STE (`output/ste.md` §Simplified Technical English), concise and direct, with no filler.

1. **Re-rank** within each severity by the persona's `emphasis` list, §1 ("What they consistently flag"), §6 ("Business and domain priorities"), and §9 ("Learned preferences" with type `emphasis` or `check`).
2. **Lets slide and learned suppressions.** LOW and non-blocking findings that match §2 ("What they let slide") or a `suppress` entry in §9 ("Learned preferences") fold into one line: `Minor items that do not block merge: <a>, <b>, <c>.` Drop them entirely if the persona or learned preference says the owner never raises that kind of item. Never drop or fold a verified CRITICAL or HIGH finding.
   - Findings in the persona's **Chat-only** list stay in the chat report, but are marked `chat-only` and left out of the posted inline comments.
3. **Severity labels.** Use the fixed STE section headings (`output/ste.md` §Simplified Technical English, "Fixed headings and phrases"). The persona's severity vocabulary (`persona.md` §3) selects the STE form from the phrase-mapping table. It does not supply non-STE words. The comment prefixes come from the `voice.*` keys. The finding ID letter keeps the true severity (C/H/M/L/Q).
4. **Tone of communication and voice.** ASD-STE100 grammar rules govern sentence construction, and the persona (`voice.tone`, §4, and §9 `tone` preferences) governs the communication style, structure, and emphasis:
   - `direct` (default): state the problem, evidence, impact, and imperative fix command directly.
   - `constructive`: pair each problem statement with a clear explanation of the benefit and, whenever possible, a concrete ```suggestion code block.
   - `mentor`: include one short STE sentence explaining the underlying architectural or domain principle before the fix command.
   - `strict`: emphasize contract boundaries, test requirements, and risk in the impact sentence.
   - `concise`: keep each comment to the minimum STE sentences needed (1 problem sentence with `path:line` evidence + 1 imperative fix command).
   - There are two renderings of each finding:
     - The **chat report** uses the report template: severity emoji and ID, `[SEVERITY / CONFIDENCE]`, a bold title, `path:line`, the issue and impact, then **Fix:** or **Ask:**.
     - The **posted inline comment** uses the inline form, written in STE:
       - CRITICAL/HIGH/MEDIUM are prefixed with `voice.comment_prefix`, with `{SEVERITY}` and `{CONFIDENCE}` filled in.
       - LOW is prefixed with `voice.nit_prefix` (default `Nit:`).
       - QUESTION has no prefix.
       - Emoji follow `voice.emoji` (default `none`). `sparse` means at most one, and only on LOW or QUESTION comments.
       - Keep within the persona's length norms and the STE sentence limits. Use a ```suggestion block for drop-in one-line fixes.

   The rewrite changes only the presentation and structure:
   - Keep the technical claim, the evidence and the fix exactly. Each `path:line`, identifier, number and fix action must still be in the STE text.
   - Do not add opinions, jokes or phrases the persona file does not support.
   - Where the persona's evidence is `low confidence`, stay close to neutral.
5. **Language and spelling.** English with American spelling (Merriam-Webster), because STE uses it. This applies whatever `voice.spelling` says. Verbatim items (`output/ste.md` §Simplified Technical English) stay as they are.
6. **Opening and closing.** Use the fixed STE verdict phrases for the verdict line. Add no greeting and no sign-off. The only closing line is the STE acknowledgement at the end of the posted review body (`output/posting.md` §Posting step 3).
7. **Safety floor.** A verified CRITICAL or HIGH correctness, security or data-loss finding cannot be dropped, folded or softened below the blocking heading. It can only be reworded.
8. **Self-authored PR.** If `author_is_me`, write the comments as notes to self ("Fix this before you request a review."). Keep the same content.
9. **Posted volume cap (Gate G7).** Post **at most `voice.max_inline` inline comments** (default 8), at most 8 MEDIUM findings in the whole review, and at most 3 LOW findings from house rules. Set it from the persona's observed comments per PR, not from how many findings there are:
   - Rank by severity, then by the persona's `emphasis`, §1 order, and §8/§9 rules.
   - Move excess non-blocking items strictly to `Not posted (chat only):` in chat. Never dump excess items into an `"Also:"` grab-bag in the posted PR body.
   - Verified CRITICAL and HIGH findings always get an inline comment, or a body bullet if their line isn't in the diff (adherence intent findings only), even past the cap.
   - The chat report still lists `post` and `chat-only` findings.
10. **STE self-check.** Run Gate G8 and the checklist in `output/ste.md` §Simplified Technical English on the whole report before you print it.

## Interactive persona and preference configuration (via `/review`)

When the user invokes `/review` to configure or update their persona or preferences (or asks to set up their review persona):
1. Read `reference/persona.md`.
2. Ask the user (or apply what they already stated in their prompt) for:
   - **Tone of communication (`voice.tone`)**: `direct`, `constructive`, `mentor`, `strict`, or `concise`.
   - **PR emphasis (`emphasis` and §1/§6)**: which concerns matter most on PRs (for example `security`, `backward-compatibility`, `test-coverage`, `performance`, `domain-modeling`).
   - **What to let slide (§2)**: minor items to fold or ignore.
   - **House rules (§8)**: objective yes/no checks to run on every diff.
   - **Memory acquisition (`memory`)**: `enabled`, `ask`, or `disabled`.
3. Update `reference/persona.md` with `status: approved` (or `draft` if the user wants to test it first) and set `refreshed` to today's `YYYY-MM-DD`. Write no personal names, emails, or handles.
