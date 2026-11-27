# Finding schema

Every lane returns this shape, and `scripts/merge_findings.py` merges on it. The shape extends the common seven-field review finding (severity, dimension, title, location, observation, impact, suggestion), so a lane that uses another reviewer's native shape maps onto it with no translation loss.

## Finding

```
Finding {
  id: string              // lane-local, e.g. "ad-3". After carry, `id` holds the stable id (e.g. "B1", see `schema/carry.md` §Stable ids) and `display_id` holds C1/H1/M1/L1/Q1
  lane: adherence | code-quality | security | performance | compatibility | robustness | conventions | skim | critical-risk | evaluation   // legacy alias: business-logic
  dimension?: string      // native dimension from an inherited method (e.g. "error handling", "STRIDE: Tampering")
  severity: CRITICAL | HIGH | MEDIUM | LOW | QUESTION
  confidence: HIGH | MEDIUM | LOW
  title: string           // ≤ 40 chars
  location: string        // PR: path:line[-end] on an added line of diff.patch, or path @@ -a,b +c,d @@ hunk header (broken callers anchor on the causing diff line and cite caller in issue/trigger); "PR description" / "intent.md §..." for intent lanes only. CODEBASE: path:line[-end] within file-map.json; "Codebase architecture" / "Codebase architecture §..." / "intent.md §..." for intent lanes only
  locations?: string[]    // extra valid sites of the SAME flaw (diff.patch at PR, file-map.json at CODEBASE; one finding for a repeated pattern)
  issue: string           // what was observed; one or two sentences
  trigger: string         // concrete input or state that makes it fail, then the wrong result. Required for CRITICAL and HIGH. "n/a (convention)" is accepted only when lane is "conventions"
  introduced_by_pr: yes | codebase   // "yes" at PR (when flawed line is added/changed by diff.patch or diff edit directly causes it; "pre-existing" is dropped by G2 at PR). "codebase" at CODEBASE
  impact: string          // why it matters (user, data, money, rollout, maintainers)
  fix: string             // concrete, actionable change (for QUESTION, include ≤1 STE sentence refresher + direct question)
  code_example?: string   // required for CRITICAL, optional for HIGH
  ac_ref?: string         // AC3 / B2 / T1 / P1, or a spec item id, that this finding relates to
  breaking?: { consumers: string, migration: string }  // external contract change
  sources: string[]       // e.g. ["security", "code-quality"] — filled by merge_findings.py
  verified?: confirmed | plausible | refuted | unverified   // filled by Phase 2.2 / finalize. confirmed = traced and true. plausible = not refuted, depends on input, config or state the code cannot show. refuted = false (removed). unverified = nobody checked it (or the verifier said unclear). Only confirmed has the safety floor. A plausible or unverified CRITICAL/HIGH still counts in open / blocking_signals
  in_diff?: bool          // filled by merge_findings.py (Phase 2.1, G2): true for non-intent code locations (and false for open.json re-raises outside a delta diff)
  dedupe_key?: string     // optional or filled during merge: "<category>:<symbol or file>"

  // Evaluation fields (Phase 2.2, Gate G10). All are optional.
  eval_disposition?: post | chat-only | drop  // filled by evaluation lane; only "post" reaches PR comments and breaker open counts / blocking_signals
  eval_reason?: string                        // one-sentence evaluation rationale
  eval_downgraded?: bool                      // true when evaluation lowered an inflated non-floor severity

  // Carry fields. All are optional. A finding without them behaves as before. See `schema/carry.md` §Stable ids and `schema/carry.md` §Carried state.
  status?: todo | fixed | declined   // filled by carry_findings.py or merge_findings.py finalize. A new finding is todo
  origin?: new | existing            // new = active finding; existing = carried historical baseline
  reopened_from?: string             // sha of the earlier fix, when a fixed finding comes back. Filled by carry_findings.py
  challenges?: string                // a lane sets it: the id of an earlier decline that this finding answers. Put the answer in `evidence`
  evidence?: string                  // with `challenges`: the code fact that answers the decline reason
  challenge?: string                 // filled by carry_findings.py on the decline: the evidence of the challenge
  reason?: string                    // why a finding was declined
  commit?: string                    // the sha of the fix, when status is fixed
  display_id?: string                // report label C1/H1/M1/L1/Q1 assigned on todo findings by carry_findings.py / merge_findings.py finalize
}
```

**Confidence meanings** (Gates G3–G4 drop or downgrade findings whose confidence or trigger does not support the severity):
- `HIGH` means you read the flawed code and traced at least one caller, guard or test. You can state the `trigger`.
- `MEDIUM` means you saw the pattern in the diff (or codebase) but did not trace callers or guards.
- `LOW` means a heuristic or a suspicion. LOW confidence is allowed only for QUESTION, or for a CRITICAL or HIGH that verification must confirm.

**Calibration rules** (`merge_findings.py` and `breaker.py` enforce these):
- CRITICAL and HIGH need a concrete `trigger` (only `conventions` may use `"n/a (convention)"`). Without one, Gate G3 (`downgrade`) caps severity at `MEDIUM`, or `QUESTION` when confidence is `LOW`.
- At `level=PR`, pre-existing defects in unchanged code are out of scope and must never be returned; Gate G2 drops any finding whose location sits outside `diff.patch` (except `open.json` re-raises in delta mode). At `level=CODEBASE`, pre-existing code is in scope and Gate G2 validates `path:line[-end]` against `file-map.json`.
- A repeated pattern is one finding with `locations`. Do not return one finding per site.
- Anything that the repo's linter or formatter enforces is not a finding (Gate G6).

**Severity meanings**:
- `CRITICAL` means exploitable security, data loss or corruption, money wrong, or a core promised requirement/AC contradicted. It blocks merge.
- `HIGH` means a real bug or a missing promise/AC on a main path, a breaking contract with no migration, or a merge-order hazard. It should block.
- `MEDIUM` means an edge case, an untested core AC (other untested ACs only fill the matrix `test` column), a maintainability risk with real cost, or an out-of-scope behaviour change. Fix it before or soon after merge.
- `LOW` is a minor improvement or a nit.
- `QUESTION` means the reviewer needs an answer from the author before judging.

## AC row (adherence lane only)

```
ACRow {
  ref: string             // AC1 / B3 / T1 / P2
  text: string            // short paraphrase of the requirement or promise (≤ 15 words)
  status: Met | Partial | Missing | Contradicted | Not verifiable
  evidence: string        // path:line at head_sha, or "—"
  test: string            // path:line of covering test, or "untested"
  note?: string           // one line, only when status ≠ Met
}
```

## Lane result

Every Phase 1 lane returns **only** one compact fenced `json` block matching `reference/lanes/lane-brief.md` §Return (omitting optional `null`/empty fields like `ac_matrix` except on `adherence`; include `"shard": "<shard_id>"` when sharded at `level=CODEBASE`, and `"challenges"`/`"evidence"`/`"cleared"` only when `declines.json` or `open.json` exist):

```json
{
  "lane": "<lane>",
  "shard": "<optional shard_id at CODEBASE>",
  "status": "returned",
  "reason": null,
  "coverage": "read: lane-brief, <method>; <scope/limits>",
  "high_risk": null,
  "safety_alerts": [],
  "other": [],
  "strengths": [],
  "cleared": [],
  "ac_matrix": [],
  "findings": [
    {
      "id": "<lane>-1",
      "lane": "<lane>",
      "dimension": "<dimension>",
      "severity": "CRITICAL|HIGH|MEDIUM|LOW|QUESTION",
      "confidence": "HIGH|MEDIUM|LOW",
      "title": "<=40 chars>",
      "location": "path:line",
      "locations": [],
      "issue": "1-2 sentences observed",
      "trigger": "input/state -> wrong result",
      "introduced_by_pr": "yes|codebase",
      "impact": "why it matters",
      "fix": "concrete actionable change",
      "code_example": "required for CRITICAL",
      "ac_ref": null,
      "breaking": null
    }
  ]
}
```

An abstained lane returns `"status": "abstained"`, a non-empty `"reason"`, and `"findings": []`. An abstained lane is a valid result, not a failed one, and can still return `"cleared"`. `"high_risk"` informs the lead and the human reviewer; it never changes a severity.

## Phase 2.1 validation (`merge_findings.py merge` / `schema_lint.py`)

Each finding is checked deterministically:
- **`retry_lane`** (re-request the lane or shard once; after a second failure mark `NOT ASSESSED`): unparseable lane JSON, missing required fields (`id`, `lane`, `severity`, `confidence`, `title`, `location`, `issue`, `fix`), invalid enums (`severity`, `confidence`, `lane`, `status`, `origin`, `eval_disposition`), `title` > 40 chars, `CRITICAL` without `code_example`, non-list or non-string `locations`, invalid optional text/bool fields, or unparseable `location` format.
- **`drop`**: Gate G2 (`introduced_by_pr: "pre-existing"` at `PR`, non-intent lane using an intent location, wrong-level intent location, or all sites outside `diff.patch` / `file-map.json`), Gate G4 (`LOW` confidence on `MEDIUM`/`LOW`), or `MEDIUM`/`LOW` findings at `effort=skim`.
- **`downgrade`**: Gate G3 (`CRITICAL` or `HIGH` with no concrete `trigger`, except `conventions` with `"n/a (convention)"`) caps severity at `MEDIUM` (or `QUESTION` when `confidence` is `LOW`; dropped at `effort=skim` if downgraded to `MEDIUM`).
