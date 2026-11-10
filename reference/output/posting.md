# Posting (Phase 4, only when posting)

## Posting (only after `--post` or the user picks a post option)

1. **Head check.** Run `gh pr view <url> --json headRefOid`. If it differs from `head_sha`, stop, say the PR moved, and offer a re-run. Never post comments anchored to a stale SHA.
2. **Diff-line map.** Parse `diff.patch` hunk headers (`@@ -a,b +c,d @@`) into the set of right-side line numbers per file that are added or context lines. A finding whose `path:line` is in that set becomes an inline comment:
   `{ "path": "<path>", "line": <line>, "side": "RIGHT", "body": "<persona comment>" }`
   For a line range, add `start_line`/`start_side`. Every other finding goes into the review body under "Other notes".
3. **Body.** Write the body in STE. At `skim`, use the reduced bullet-list body in `effort/skim.md` §Skim report ("Posted body at skim") instead of the list below. Otherwise include, in this order:
   - the verdict line (the fixed STE verdict phrase)
   - the compact AC summary (`AC: 5 met · 1 partial · 1 missing`, followed by the rows that aren't Met). Omit it at `skim`, because there is no AC matrix.
   - out-of-scope bullets
   - findings that aren't inline
   - the questions
   - the STE acknowledgement, as the last line (the fixed phrase in `output/ste.md` §Simplified Technical English). Add it one time, in the body only. Do not add it to inline comments.
   Don't include the footer. Keep it under 60k characters.
4. **Payload.** Run Gate G8 and the STE self-check (`output/ste.md` §Simplified Technical English) on the body and on each inline comment. Skip an inline comment that Gate G5 found already raised. Then write `<bundle_dir>/review-payload.json`:
   `{ "commit_id": "<head_sha>", "event": "<event>", "body": "...", "comments": [...] }`
5. **Confirm.** Show the full payload: the event, the body, and each inline comment with its path:line. Then ask with AskUserQuestion:
   - "Post exactly this"
   - "Change event to COMMENT"
   - "Edit first"
   - "Cancel"
   An `APPROVE` or `REQUEST_CHANGES` event needs this explicit confirmation every time. At effort `low`, never build an `APPROVE` payload. At effort `skim`, build an `APPROVE` payload only when `effort/skim.md` §Skim verdict allows it (the PR is in profile, CI is `passing` and `!author_is_me`).
6. **Post.** Run `gh api repos/<owner>/<repo>/pulls/<N>/reviews --method POST --input <bundle_dir>/review-payload.json`.
   - On HTTP 422 (a line isn't part of the diff), move the rejected comments into the body, show the payload again, and ask again.
7. **Result.** Print the review's `html_url`. Nothing else is posted: no reactions, no Slack messages, no issue comments.
