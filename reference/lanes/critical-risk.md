# critical-risk method (effort low)

### critical-risk method (inline pass or single lane)
```
EFFORT: low. Report CRITICAL and HIGH findings only. Do not report MEDIUM, LOW or style findings. There is no cap on CRITICAL and HIGH findings.
Read diff.core.patch and intent.md. Read file ranges on demand with git show <head_sha>:<path> | sed -n 'a,bp'. Respect the existing codebase's architecture and framework conventions; follow any convention files the codebase points to when needed to verify a claim.
Check only these, in this order:
1. Adherence: any promised requirement, task or AC that is Missing or Contradicted on a main path, and any change to a "must not change" boundary.
2. Security: missing or broken authorization on a new or changed endpoint, trust of a client-controlled header or field for access, data exposure across tenants or actors, injection.
3. Data: data loss or corruption, a migration that locks or rewrites a large table, a new index on a large table that is not built online or concurrently, a cross-service side effect before commit or with no failure behavior.
4. Contract: a removed or renamed response key, or a new required request field, on a public or external API.
5. House rules & learned preferences: each persona §8 or §9 rule at CRITICAL or HIGH whose `applies_when` holds. Skip this item when §8 and §9 have no rules.
6. Correctness: a crash, wrong result or silent data drop on a main path.
Return the Lane result block with AC_MATRIX rows for Missing and Contradicted only (no test column), and STRENGTHS: none.
```
When `--lanes` or `--no-inherit` narrows the focus, keep item 1 and only the items that match the named lanes.
Carry at `low`: when `declines.json` and `open.json` exist, the lane (or the inline pass) reads both and follows `schema/carry.md` §Carried state. `rules-changed.txt` is written with the base state `skipped`.
