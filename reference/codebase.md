# Codebase review (`level=CODEBASE`)

Read this file **only** when the user explicitly requests `level=CODEBASE` (or `--level=CODEBASE`). By default, `/review` **always** runs at `level=PR` and never loads this file.

## Scope & Exhaustiveness Contract

`level=CODEBASE` exhaustively audits the entire repository (or an optional repo-relative `<path>` subtree passed as `target`) using the **same 7 specialist lanes** (`adherence`, `code-quality`, `conventions`, `security`, `performance`, `compatibility`, `robustness`), the same ownership boundaries (`lanes/selection.md`), blind verification (`lanes/verifier.md`), Gate G10 evaluation (`lanes/evaluation.md`), deterministic Carry (`G9`), Breaker (`breaker.py --level CODEBASE`), and Gate G8 STE lint (`ste_lint.py`).

- **Default effort:** `--effort=auto` always resolves to **`high`** (exhaustive). `--effort=medium` and `--effort=low` work when explicitly requested. `--effort=skim` is rejected at `CODEBASE`.
- **All 7 lanes in parallel:** At `high`, the lane cap is **7** with **no micro-lane auto-folding** and **no small-PR cap**. Every lane runs on `standard` (`inherit`) tier. Only lanes with 0 eligible files in scope skip.
- **Deterministic directory sharding:** When a lane's file slice exceeds **30 files or 6,000 LOC** (`--shard-max-files` / `--shard-max-loc`), `build_bundle.py` partitions it by top-level directory/module into `lanes/<lane>-shard-<k>.files.txt`. Launch one subagent per shard entry in `lane_plan.selected` (each returns `"lane": "<lane>"` and `"shard": "<shard_id>"`). `merge_findings.py merge` aggregates shard statuses per lane and deduplicates findings across shards.

## Phase 0: Codebase Bundle, Intent & Plan

1. **Run `build_bundle.py --level=CODEBASE` (1 tool call):**
   ```bash
   python3 <skill_dir>/scripts/build_bundle.py [scope_path] --level=CODEBASE [--effort=auto|high|medium|low] [--fresh] [--lanes=a,b] [--no-inherit] [--no-tier] [--spec=<path|url>]
   ```
   - Never fetches GitHub PR metadata or comments (`G0`/`G0b`/`G5` skipped).
   - Builds `file-map.json` (`{"<path>": {"lines": N, "lang": "..."}}`), `files.txt`, `signals.txt`, `rules-discovered.txt`, `lanes/<lane>.files.txt` (+ shard `.files.txt` when sharded), `lanes/manifest.json`, and `bundle-summary.json`.
   - Prepares Carry state from `~/.claude/review/reviews/<owner>-<repo>-codebase-*/findings.json` (unless `--fresh`; never delta-diffed).
2. **Build codebase `intent.md`:**
   - Read `intent_hints.cli_specs` and `intent_hints.repo_specs` (repo specs, plans, ADRs, RFCs, `README.md`, `ARCHITECTURE.md`, `DESIGN.md`, `SECURITY.md`) at `head_sha` (never a dirty working tree when `clone` exists).
   - Extract documented product promises, architecture invariants, public contracts, and "must not change" boundaries into `<bundle_dir>/intent.md`.
   - Do **not** ask the PR-description `AskUserQuestion` prompt when no specs exist; record `Gaps: no formal spec files discovered; adherence checked against README/docs and codebase contracts` and proceed.
3. **Print STE codebase review plan:**
   ```
   Review plan — <repo> @ <sha7> (level: CODEBASE, scope: <scope_path>, <files> files, <lines> lines) · Effort: <effort>
   Intent: <spec/doc paths | "README/docs and codebase contracts">   Gaps: <list or none>
   Lanes (<k> tasks across <m> lanes): <lane>[ (<s> shards)] — <why>; ...
   Skipped: <lane> — <why>; ...
   Carry: <n> findings from <sha7> (<d> declined, <o> open CRITICAL/HIGH) | none | skipped (--fresh)
   ```
   Proceed immediately without asking.

## Phase 1: Parallel Exhaustive Lane Fan-Out

Launch every entry in `lane_plan.selected` in one message (or in clean parallel waves if sharded count is very large), using `lanes/prompt.md` with these `CODEBASE` substitutions:
- Set `LEVEL: CODEBASE` (and `SHARD: <shard_id> (<scope>)` when sharded).
- Replace the `PR:` line with: `REPO: <owner>/<repo>  head <head_sha>  scope <scope_path>`.
- Replace `lanes/<lane>.patch` in `BUNDLE` with:
  - `<files_list>` (`lanes/<lane>.files.txt` or `lanes/<lane>-shard-<k>.files.txt`): **YOUR ASSIGNED FILE LIST — read every listed file exhaustively at `head_sha`** (priority signal-matched files appear first).
  - `file-map.json`: valid repo-relative paths and line counts.
  - `files.txt`: full inventory of codebase files in scope.
  - `rules-discovered.txt`: all discovered repository rule and convention files (include for `conventions` and `adherence`).
  - `intent.md`: repository specs, architecture docs, and documented contracts (include for `adherence`, `compatibility`, and `conventions`).
  - `declines.json` / `open.json` (only when present).
- **Finding rules in `CODEBASE` mode:**
  - Read files via `git -C <clone> show <head_sha>:<path>` (or direct workspace read when `clone` is `none`). Never read denylisted secrets (`.env*`, `*.pem`, `credentials.json`).
  - Set `"introduced_by_pr": "codebase"` (and `"shard": "<shard_id>"` on the lane JSON object when sharded).
  - Anchor `location` on a real `path:line` inside `file-map.json`. Group repeated patterns across files into one finding using `locations`.
  - Report concrete bugs, security vulnerabilities, performance bottlenecks, contract/migration hazards, robustness/concurrency/resource-leak defects, convention/rule drift, and high-value structural improvements. Skip linter-enforced style nits and idiomatic framework patterns.
  - Per-lane/shard non-blocking cap at `CODEBASE`: **15** `MEDIUM`/`LOW`/`QUESTION` findings ranked by impact × likelihood (no cap on `CRITICAL`/`HIGH`; put extra titles in `other`).

## Phase 2: Merge, Overlapped Verify/Eval Wave & Finalize

1. **Deterministic Merge (`merge_findings.py merge`):**
   ```bash
   python3 <skill_dir>/scripts/merge_findings.py merge <bundle_dir> --level=CODEBASE
   ```
   - Gate G2 validates every `location` against `file-map.json` (file exists in scope and `1 <= start <= end <= lines`); out-of-bounds/missing paths drop cleanly.
   - Gate G5 is skipped (no PR comments). Gate G6 (CI linter/typechecker configs) still drops linter-enforced nits (never `CRITICAL`/`HIGH`).
   - Sharded lanes aggregate into one logical status per lane for `breaker.py` (any `NOT ASSESSED` shard marks that lane `NOT ASSESSED`).
   - Up to **8** blind verifier batches are created at `high` (`2` at `medium`).
2. **Overlapped Parallel Wave (same as `PR`):**
   - Launch Blind Verifiers (`lanes/verifier.md`) and Evaluation (`lanes/evaluation.md`, passing `LEVEL: CODEBASE` and `file-map.json`) concurrently in one message on `standard` (`inherit`) tier.
   - In `CODEBASE` mode, Evaluation checks file/line validity and real improvement value (never dropping findings for being pre-existing), spot-checks `MEDIUM` evidence at `head_sha`, drops speculative/idiomatic/redundant pedantry, routes informational notes to `chat-only`, and preserves the safety floor.
3. **Deterministic Finalize (`merge_findings.py finalize`):**
   ```bash
   python3 <skill_dir>/scripts/merge_findings.py finalize <bundle_dir> --level=CODEBASE [--carry]
   ```
   - Runs `breaker.py --level CODEBASE`, returning codebase health verdicts:
     - `NEEDS ACTION` (🔴 `"Codebase needs urgent fixes"`)
     - `IMPROVEMENTS RECOMMENDED` (🟡 `"Codebase needs targeted improvements"`)
     - `HEALTHY` (🟢 `"Codebase is in good shape"`)
   - `event` is always `"NONE"`. Quality-gate band (`PASS` / `CONCERNS` / `FAIL`) covers `code-quality`, `performance`, and `security`.

## Phase 3 & 4: Report, Durable Archive & Local Follow-Up

- **Phase 3:** Skip `persona-pass.md` on `zero_findings_fast_path` (`HEALTHY` with zero findings). Otherwise run Persona Pass without Gate G7 PR-inline truncation (all prioritized `post` improvements remain in the report; group `LOW` items concisely by lane/theme). Run Gate G8 (`python3 <skill_dir>/scripts/ste_lint.py <bundle_dir>/report.md --findings <bundle_dir>/reported.json`).
- **Phase 4 Report Template (ASD-STE100):**
  ```markdown
  ## <🔴|🟡|🟢> <health verdict phrase> — <repo> @ `<sha7>` (Codebase review)
  <one-sentence reason>
  **Top items:** <the 3 highest `post` findings as `ID title — path:line`, or "none">. **Counts:** <c> critical · <h> high · <m> medium · <l> low · <q> questions · <chat> chat-only

  **Scope:** level `CODEBASE` · head `<sha7>` · path `<scope_path>` · <N> files (<LOC> lines) · specs <paths | none> · rules <paths | none>
  **Gaps:** <list, or "none">
  **Review safety:** <chat-only alerts if any; omit when none>
  **Earlier declines still present:** <if any; omit when none>

  ### ⚠️ Contract and compatibility risks
  <breaking/contract findings if any; omit when empty>

  ### Spec and documentation drift
  <adherence AC/promise table for non-Met rows if specs/docs checked; omit when empty>

  ### Must fix (CRITICAL, HIGH)
  **C1 · <title>** — `path:line`
  <STE problem, evidence, impact, and Fix commands>

  ### Recommended improvements (MEDIUM)
  **M1 · <title>** — `path:line`
  <STE comment>

  Additional notes (chat only):
  - **M9 · <title>** — `path:line` — <one STE sentence>

  ### Low-priority improvements (LOW)
  **L1 · <title>** — `path:line` — <one STE sentence>
  Minor improvements: <folded items>

  ### Open questions
  1. **Q1** (`path:line`) — <at most 1 STE sentence explaining the item>. <Direct question?>

  ---
  Level: CODEBASE · Effort: <high|medium|low> · Lanes: <lane ✓ | lane NOT ASSESSED (reason) | lane skipped (reason)> · Quality gate: <PASS|CONCERNS|FAIL|n/a> · Findings: <raw> raw → <merged> merged → <post> kept (<chat_only> chat-only) · Gates removed: <g2> invalid-location, <g3> downgraded, <g4> low-confidence, <g6> linter, <refuted> refuted, <eval> evaluated-out · Persona: <refresh date>, <approved | unapproved (draft) | neutral (none)> · Language: ASD-STE100, G8 clean
  ```
- **Durable archive:** Copy `report.md`, `ac-matrix.md` (if present), `findings.json`, and `breaker-input.json` to `~/.claude/review/reviews/<owner>-<repo>-codebase-<sha7>/`.
- **Interactive follow-up (`AskUserQuestion`, never offer GitHub PR posting; always pair every cited ID with ≤1 STE sentence):**
  - `"Fix top findings now (Recommended)"`
  - `"Show fix plan for <ID> — <≤1-sentence STE refresher>"`
  - `"Mark findings as declined or fixed"` (runs `carry_findings.py mark`)
  - `"Configure persona or save preferences"`
  - `"Done"`
