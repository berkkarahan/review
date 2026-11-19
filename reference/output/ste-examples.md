# STE: persona phrase mapping and examples

**Persona phrase mapping.** The persona decides what to flag, the order, the severity and the length. STE decides the words. Use this table when a persona phrase (`persona.md` §3–§5) does not agree with STE:

| Persona form | STE form |
|---|---|
| "We shouldn't do this." · "I think this needs fixing" (blocking) | A problem sentence, then a command: "This <does X>. <Do Y>." |
| "Can we …?" · "How about …?" · "Maybe we can …" · "i would prefer X over this" (MEDIUM) | A command: "<Verb> …". The prefix shows that it does not block. |
| "not a blocker" · "feel free to ignore" · "personal preference" (LOW) | The `NIT:` prefix and a command. When the persona marks the item as optional, add "This is optional." |
| "intended?" · "is this expected?" · "Do we need …?" · "why do we need …?" (QUESTION) | One context sentence and one direct question: "Is this change intended?" |
| "I am not sure but …" | LOW confidence in the prefix, and "This is not verified." |
| "we usually do;" + bullets | "We usually do this:" + bullets |
| "better now than later" | "Do this before merge." |
| "just make sure it will not fail" | "Make sure that <X> does not fail." |
| "wdyt?" | Remove it. If you need a reply, ask a specific question. |

**Examples** (invented for this file):

A HIGH inline comment.
- Before:
  > [HIGH / HIGH] I think this needs fixing; AC-4 says a draft is "changed since the last release" (spec.md:88), and a rename is a real unreleased change: the list serves the released name (`documents/models.py:142`). Can we state the semantics, align :20/:31, turn AC-4 into assertions and close OQ-2?
- After:
  > [HIGH / HIGH] This spec leaves the AC-4 behavior open, but the requirement gives the answer. AC-4 applies when a draft is "changed since the last release" (spec.md:88). The list shows the released name, so a rename is an unreleased change (`documents/models.py:142`). Do these steps:
  > - Write the rule: each write to the draft sets the flag, except archive and unarchive. Release clears the flag.
  > - Change :20 and :31 to agree with the rule.
  > - Change AC-4 to assertions.
  > - Close OQ-2.

A LOW comment.
- Before:
  > NIT: `allow_rename` requires `allow_edit` (400), worth saying here and in docs/api.md:47.
- After:
  > NIT: The API rejects `allow_rename=true` when `allow_edit=false` (400, `api/validators.py:51-55`). Add this rule to this note and to docs/api.md:47.

A QUESTION (author question with ID citation).
- Before:
  > Should we change AC-4 before merge, or wait for OQ-2?
- After:
  > **Q1** (`documents/models.py:142`) — AC-4 requires released drafts to show updated names, and OQ-2 tracks whether renames trigger a new draft. Should we change AC-4 before merge, or wait for OQ-2?

A human-in-the-loop prompt (`AskUserQuestion` / follow-up choice).
- Before:
  > Should we post X1 and drop Y2? Options: "Show fix for C1", "Drop H2", "Decline B1".
- After:
  > X1 reports an unchecked null dereference in `auth/session.py:44`, and Y2 flags a duplicate cache lookup in `orders/repo.py:19`. Should we post X1 and drop Y2?
  > Options: "Show fix for C1 — adds a null guard in `auth/session.py:44`", "Drop H2 — ignores the timeout fallback in `jobs/sync.py:88`", "Decline B1 — keeps the current rate limit for `POST /login`".

Another QUESTION (issue reference).
- Before:
  > Issue #120 and web#3415 still promise shared drafts and "reuses the editor"; updated before merge, or in a later pass?
- After:
  > **Q2** (`PR description`) — Issue #120 and web#3415 still say that users can share drafts and reuse the editor. Will you update these issues before merge, or later?
