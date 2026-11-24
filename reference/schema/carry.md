# Carry schema (stable ids and carried state)

## Stable ids

The id of a finding stays the same from one review of a PR or codebase (`level=CODEBASE`) to the next. Three kinds of id exist. Do not mix them.

| Kind | Example | Set by | Lives in |
|---|---|---|---|
| Lane-local id | `ad-3` | the lane | the lane result, until the carry step (except re-raised `open.json` items, which keep their stable id) |
| Stable id | `B1`, `S2` | `scripts/carry_findings.py` | `findings.json`, `declines.json`, `open.json`, `cleared` |
| Report label | `H2` | `carry_findings.py` (`display_id`) or the lead | the report and the posted comments |

A stable id is one lane letter and a number. No letter is C, H, M, L or Q, so a stable id never reads as a report label.

| Letter | Lane |
|---|---|
| B | adherence (legacy alias: business-logic) |
| D | code-quality |
| S | security |
| P | performance |
| A | compatibility |
| R | robustness |
| V | conventions |
| K | skim |
| X | any other lane (for example `critical-risk`), or no lane |

Rules:
- A new finding takes the next number for its lane letter from `next_id`. `next_id` is an object of letter to number. It moves past every id that it assigns.
- An id is never reused. `next_id` also moves past every id that appears in `findings`, `cleared` or `previous`, whatever the status.
- A carried finding keeps its id. A finding that is raised again keeps its id. A fixed finding that comes back takes its earlier id.
- A finding that two lanes report becomes one finding. It takes the letter of the lane that the merge keeps.
- An earlier `findings.json` with report labels as ids (C/H/M/L/Q, or no id) is a legacy file. The script gives each of its findings a stable id once, and prints the old-to-new map as `id_map`.

## Carried state

`~/.claude/review/reviews/<owner>-<repo>-<N>-<sha7>/findings.json` (at `level=PR`) or `~/.claude/review/reviews/<owner>-<repo>-codebase-<sha7>/findings.json` (at `level=CODEBASE`) keeps the shape in `output/report.md` §Durable copy, plus these keys:

```
{
  "pr": "<owner>/<repo>#<N>" | "<owner>/<repo>#codebase", "last_sha": "<head_sha>",
  "next_id": {"B": 3, "S": 2},
  "findings": [ Finding with id and status, ... ],     // every status: todo, fixed and declined
  "cleared": [ {"id": "S1", "evidence": "...", "lane": "security"} ],
  "previous": [ {"id": "B2", "outcome": "resolved | still open | changed"} ],
  "reconcile": { ...counts and id lists of the last carry step... }
}
```

Status lifecycle:

| Status | Meaning | Set by |
|---|---|---|
| `todo` | Open. The review reports it. | `carry_findings.py` (including `merge_findings.py finalize --carry` on Round 1) for every finding that a lane raises |
| `fixed` | The author fixed it. `commit` holds the sha. | `carry_findings.py mark` (the owner or the lead names the fix), or a resolved clear (`carry/rereview.md` §Carry rules) |
| `declined` | The finding was dropped for a stated `reason`. | `carry_findings.py mark` (Phase 4 `dropped-by-user`, or an author reply that the lead confirms) |

Write only short, plain reasons. Put no names, handles or emails in `reason`, `challenge` or `evidence`. `carry_findings.py` redacts emails and `@handles` (including `_`, `.`, `@org/team`, and `@name[bot]`) in `reason`, `challenge`, `evidence`, durable finding prose fields, `notes`, `warnings`, `declines.json`, and `open.json`. It cannot find a person's plain name. The lead must not write one.

Two files go to the lanes from the bundle. Each is a JSON list. Each row has exactly these keys, and no other text from a comment:

```
declines.json  [ {id, file, lines, severity, issue, reason} ]          // status declined
open.json      [ {id, lanes, file, lines, severity, issue} ]           // status todo, CRITICAL or HIGH
```
(`lanes` includes `critical-risk` and `evaluation` when applicable and maps prefixed `sources` such as `checklist:security` -> `security`.)

Lane contract for these files. The lead pastes these two rules into each lane prompt when the bundle holds the files:
- A finding that matches a decline is not raised, unless the decline `reason` is wrong against the code. Then set `challenges` to the decline id, and make `evidence` answer the reason.
- For each open finding that lists this lane, read the current code and either re-raise it with `id` set to the entry's stable `id` (matched first by `carry_findings.py reconcile`) when the flaw is still there, or return it in `CLEARED` with `path:line` evidence from the code at `head_sha`. A finding that the lane neither raises again nor clears stays open. A miss never closes it.
