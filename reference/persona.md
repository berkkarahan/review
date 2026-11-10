---
status: empty          # empty | draft | approved. With "empty", the review is persona-neutral.
refreshed: null        # YYYY-MM-DD of the last update
memory: ask            # enabled | ask | disabled — gather and save user preferences during reviews
voice:
  tone: direct         # direct | constructive | mentor | strict | concise | neutral
  spelling: american   # american | british
  max_inline: 8        # posted inline comments at most
  hold_event: REQUEST_CHANGES   # REQUEST_CHANGES | COMMENT (event for a "not ready" verdict)
  comment_prefix: "[{SEVERITY}]"   # {SEVERITY} and {CONFIDENCE} are filled in
  nit_prefix: "Nit:"
  emoji: none          # none | sparse
emphasis: []           # e.g. [security, backward-compatibility, test-coverage, performance]
---

# Reviewer persona and preferences

This file configures the review persona and stores learned user preferences. With `status: empty` and no rules in §8 or §9, the review uses no persona voice and no custom rules. Users can configure this file interactively via `/review` or edit it directly, setting `status: draft` (shown in the report footer) or `approved`, and updating `refreshed`.

Rules for this file:
- Write no names, emails, handles, user ids, customer names or account ids.
- Paraphrase. Do not paste real comments verbatim.
- The persona shapes the emphasis, comment structure, tone of communication, and final write-up (`output/persona-pass.md` §Persona pass) within ASD-STE100 grammar rules (`output/ste.md`). It never drops or lowers a verified CRITICAL or HIGH finding.
- §8 ("House rules") and §9 ("Learned preferences") are also read by the `conventions` lane to run objective checks and respect user-specific suppressions.

## 1. What they consistently flag

Ranked list of topics emphasized on PRs. For each item: the topic, how often (often | sometimes | rare), and confidence (high | medium | low). Also list high-level areas in the frontmatter `emphasis` array so lane selection prioritizes them.

_(empty)_

## 2. What they let slide

Topics that the reviewer does not comment on, or folds into one "Also:" line.

_(empty)_

## 3. Severity vocabulary

How the reviewer names each level (for example "blocker", "should fix", "nit", "question"). The persona pass maps these onto the fixed STE headings.

_(empty)_

## 4. Tone and structure

Controls the tone of communication (`voice.tone` in frontmatter plus notes below), comment length, whether the reviewer explains the architectural "why" (`mentor` / `constructive`), uses direct commands (`direct` / `concise`), includes drop-in code suggestions, and how comments are structured—while keeping sentences compliant with ASD-STE100 grammar rules.

_(empty)_

## 5. Recurring phrasings (paraphrased templates)

Short paraphrased patterns, mapped to STE in `output/ste-examples.md`.

_(empty)_

## 6. Business and domain priorities

What matters most to this reviewer on PRs at the design and architecture level (for example data safety, backward compatibility, rollout safety, test coverage, domain boundaries).

_(empty)_

## 7. Provenance

How this file was configured or updated: interactively via `/review`, from review feedback (`memory`), or by hand, and when. No names and no repository names that identify an organisation.

_(empty)_

## 8. House rules (objective checks for the conventions lane)

Each rule is a yes/no test against the diff. Cite it as `persona §8 #n`. The severity in brackets is the default. Leave the list empty to run no house rules.

Format of one rule:

```
<n>. <a yes/no check against the diff>. [<CRITICAL|HIGH|MEDIUM|LOW>] · <confidence>
```

Example (delete it, or keep it as your rule 1):

```
1. The PR mixes schema or data migrations with feature code, and they should be a separate PR. [MEDIUM] · high
```

_(no rules)_

### Applicability and status (read by the conventions lane)

The conventions lane skips a rule when its `applies_when` is false for the PR, when its `status` is `disabled`, or when its `owner_lane` is a lane that ran (`lanes/selection.md` §Lane ownership). Signals are the names in `signals.txt`. `owner_lane` is `none`, `performance`, `compatibility` or `robustness`.

| # | applies_when | owner_lane | status |
|---|---|---|---|

## 9. Learned preferences (acquired via memory)

When `memory` is `enabled` or `ask`, the review skill gathers important user preferences during reviews (for example, when the user drops a category of finding, states a project convention, or asks to emphasize a check) and saves them here. Cite a learned preference as `persona §9 #n`.

Format of one learned preference:

```
<n>. [check | suppress | emphasis | tone] (<scope: all | repo:<owner>/<repo> | lang:<lang>>) <concise rule or preference>. [<severity or n/a>] · learned <YYYY-MM-DD>
```

_(no learned preferences)_
