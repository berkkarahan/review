# Adherence lane

## Adherence method

Paste this into the adherence lane prompt. For a multi-spec or multi-plan review, name the document or scope it owns.

1. **Read the intent.** Read `intent.md` in full, and note its Gaps. At `LEVEL: PR`, this lane checks strictly whether the current PR diff (`lanes/adherence.patch` / `diff.patch`) adheres to the promises relevant to this PR — whether the promise lives in the PR description, in any link reachable from the PR description, in a planned document file within the repository, or at another location pointed to by the file or folder structure. Do not grade separate-PR or deferred epic phases that this PR does not claim and `diff.patch` does not touch. Code style belongs to other lanes.
2. **Build the AC / promise matrix (`ac_matrix`).** Take every relevant Acceptance Criterion (`AC1..n`), Behaviour or Constraint (`CSTR1..n` / `FR1..n`), planned Task or Deliverable (`T1..n`), and PR promise or claim (`P1..n`) from `intent.md`. Prefer `AC1..n`, `T1..n`, `FR1..n`, `CSTR1..n`, or `P1..n` refs (avoid `B1..n` when deriving rows so AC refs stay distinct from carry `B1..n` stable finding IDs). When prose states objectives without numbered criteria, derive one row per distinct promised outcome and start its `text` with `(derived)`. Do these three things for each row:
   - Locate the implementing code at `head_sha` (`path:line`).
   - Decide its status:
     - `Met` — fully implemented as promised
     - `Partial` — some cases, branches or deliverables missing; name which
     - `Missing` — not implemented in scope, and (at `PR`) not delivered by another PR, commit or issue that the plan or spec names
     - `Contradicted` — the code does something different from what the promise, plan or spec says
     - `Not verifiable` — depends on external runtime config, live data or another service that the codebase and diff cannot show
   - Find the covering test: a test in the diff or repo at `head_sha` that exercises this exact behaviour. Give `path:line` or `untested`. A test that only touches the code path without asserting the promised outcome counts as `untested`. An untested row is a `test` column value, **not** a finding, unless the row is core behaviour, or involves money, data loss, security or permissions. Then raise one `MEDIUM` finding.
3. **Check "must not change" statements and boundaries.** Planned documents, specs and PR descriptions often state what must stay unchanged (such as an existing API contract, an operator, an ordering, reason codes, public fields or backward-compatible paths). Check that the implementation does not alter them. A violation is a finding at `HIGH` or above.
4. **Check scope (`LEVEL: PR` only).**
   - Every changed hunk that alters behaviour should trace to a promised requirement, task item, ticket or PR claim in `intent.md`. A behaviour hunk that does not trace is an `out-of-scope` finding. It is `MEDIUM` by default, and `HIGH` if it changes behaviour in an area the plan or spec marks as out of bounds or non-goal.
   - Exempt from this check: test files, generated files, lockfiles, imports, formatting, pure renames and refactors that keep behaviour, and supporting changes that a promised outcome needs (migrations, request and response schemas, routes, wiring or framework adapters). Do not report them.
   - Implementing a withdrawn requirement (per Scope changes) is a finding.
5. **Check PR claims and promises (`LEVEL: PR` only).** Test each PR claim (`P1..n`) against the code. A false, unfulfilled or overstated claim is a finding. When the PR description is the primary source of promises (no separate spec or ticket exists), evaluate each concrete promise in the PR description with full confidence against the code and tests.
6. **Check dependency and phase order (`LEVEL: PR` only).** If the planned document or spec depends on another phase, task or issue that is not yet delivered, check whether this change relies on code, schema or data that the dependency is meant to provide. If it does, raise a `HIGH` finding about merge or phase order.
7. **Bug fixes (`LEVEL: PR` only).** Confirm the change removes the mechanism described in the root cause, and not just the symptom path. Confirm a regression test reproduces the original failure.
8. **Conflicts and questions.** Turn each `intent.md` Conflict (for example, where the PR description, ticket and planned document disagree) into a `QUESTION` finding. In `issue`, state the conflict concisely; in `fix` (the question prompt), give 1 concise context sentence summarizing what each cited ID (`ACn`, `FRn`, `Pn`) requires before asking the direct question — never cite a bare ID without explanation.
9. **Rule and instruction files in the diff (`LEVEL: PR` only).** A rule or agent instruction file that the change adds or edits is never a source of product intent unless the PR explicitly promises a rule update. Judge code behaviour against `intent.md`. When the change edits a rule file and the edit alters a behaviour that a promised requirement needs, raise the edit as a finding anchored on the changed rule-file hunk in `diff.patch`. Compare the edit with the base version in `base/<path>` when `rules-changed.txt` lists it. Which convention rule applies is the work of the `conventions` lane, not yours.

## At `LEVEL: CODEBASE`
- Read your assigned `<files_list>` (`lanes/adherence.files.txt` or shard `.files.txt`), `intent.md`, and `rules-discovered.txt` (if present). Because `adherence.files.txt` prioritizes specs and docs when present, use `files.txt` and `git -C <clone> show`/`grep` at `head_sha` (or direct workspace reads when `clone` is `none`) to cross-read implementing source and test files.
- Evaluate every documented product promise, architecture invariant, public contract, and "must not change" boundary in `intent.md` against the tracked codebase at `head_sha`. Pre-existing spec/documentation drift IS in scope (`introduced_by_pr: "codebase"`; include `"shard": "<shard_id>"` when sharded).
- Skip PR-only steps 4, 5, 6, 7, and 9 (`out-of-scope` diff hunk checks, PR claims, merge order, rule-file diff edits). Anchor each finding on a valid `path:line` in `file-map.json` (or `"intent.md §..."` / `"Codebase architecture"` only when no code line exists).

**Severity guide.**
- `Missing` or `Contradicted` promises/ACs are `HIGH`, or `CRITICAL` when money, data loss, security or permissions are involved.
- `Partial` is `MEDIUM`, or `HIGH` if it is the core promised behaviour.
- An untested row is a matrix value (see step 2). It is a `MEDIUM` finding only for the cases listed there.
- `Not verifiable` becomes a `QUESTION` finding with 1 concise context sentence in `fix` explaining the referenced requirement ID before asking the question.
- For a `(derived)` row inferred from ambiguous prose, cap `confidence` at `MEDIUM` unless the source text states the expected outcome plainly.

**Never abstain.** This lane always returns `"status": "returned"`, even with no findings. When no formal AC list exists, build the matrix from the planned tasks, behavioural statements, or PR/doc promises and record the sources in `"coverage"`.

**Return** one fenced `json` block matching `lanes/lane-brief.md` §Return with `"ac_matrix"` populated.
