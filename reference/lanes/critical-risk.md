# critical-risk method (effort low)

### critical-risk method (inline pass or single lane)
```
EFFORT: low (thinking: brief, reach: callers). Look for CRITICAL and HIGH first. Report every other finding that has evidence, at its true severity. Skip style. There is no cap on CRITICAL and HIGH findings. Your reach is the diff plus direct callers, callees and guards. Do not search wider.
- At LEVEL: PR: read diff.core.patch and intent.md. Read file ranges on demand with git show <head_sha>:<path> | sed -n 'a,bp' strictly as read-only context to verify changed hunks. Report only defects introduced or worsened by the PR diff (never report pre-existing defects; introduced_by_pr: "yes").
- At LEVEL: CODEBASE: exhaustively read every file in your assigned <files_list> (lanes/critical-risk.files.txt) at head_sha (or workspace files when clone is none, skipping denylisted secrets), plus file-map.json and intent.md. Pre-existing CRITICAL and HIGH defects in scope are valid (introduced_by_pr: "codebase"). Anchor every location on a real path:line inside file-map.json.
Respect the existing codebase's architecture and framework conventions; follow any convention files the codebase points to when needed to verify a claim.
Check these first, in this order:
1. Adherence: any promised requirement, task or AC that is Missing or Contradicted on a main path, and any change to a "must not change" boundary.
2. Security: missing or broken authorization on a new or changed endpoint, trust of a client-controlled header or field for access, data exposure across tenants or actors, injection.
3. Data: data loss or corruption, a migration that locks or rewrites a large table, a new index on a large table that is not built online or concurrently, a cross-service side effect before commit or with no failure behavior.
4. Contract: a removed or renamed response key, or a new required request field, on a public or external API.
5. House rules & learned preferences: each persona §8 or §9 rule at CRITICAL or HIGH whose `applies_when` holds. Skip this item when §8 and §9 have no rules.
6. Correctness: a crash, wrong result or silent data drop on a main path.
RETURN: one compact fenced json block matching lanes/lane-brief.md §Return with "lane": "critical-risk", "ac_matrix" populated for Missing and Contradicted rows only (set "test": "untested" or path:line), and omit "strengths"/"other". (When run inline by the lead, write this same JSON object into <bundle_dir>/raw-lanes.json.)
```
When `--lanes` or `--no-inherit` narrows the focus, keep item 1 and only the items that match the named lanes.
Carry at `low`: when `declines.json` and `open.json` exist, the lane (or the inline pass) reads both and follows `<skill_dir>/reference/schema/carry.md` §Carried state. At `PR`, `rules-changed.txt` is written with the base state `skipped`.
