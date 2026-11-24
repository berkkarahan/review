# Lane checklist: code-quality

**Scope by `LEVEL`:** At `LEVEL: PR`, read and apply this checklist strictly to lines added, modified, or removed in `lanes/code-quality.patch` (inspect surrounding files and callers solely as read-only context to verify impact; report only defects introduced or worsened by this PR). At `LEVEL: CODEBASE`, read and apply this checklist exhaustively across every file in your assigned `<files_list>` at `head_sha` (pre-existing defects and improvements in scope are valid; set `introduced_by_pr: "codebase"`). Map each item to a `dimension` value.

**Codebase and framework awareness:** Before raising any finding, inspect the existing codebase, surrounding modules, and any convention files or framework documentation that the codebase points to. Respect the project's established architecture, layering, error-handling idioms, and test conventions. Do not flag patterns that follow the existing codebase's conventions unless they cause a concrete defect.

**Code quality** (dimensions: correctness, design, readability, security hygiene, performance hygiene, testability, accessibility, error handling)
- Correctness: wrong condition, off-by-one, null or empty case, wrong type or unit, broken invariant, dead branch.
- Design: responsibility placed in the wrong layer relative to the existing codebase architecture, duplicate of an existing helper in the repo, leaky abstraction, needless complexity.
- Readability: misleading name that contradicts the code or repo terminology, hidden side effect, a comment that contradicts the code.
- Security hygiene: unvalidated input, secret in code or logs, unsafe default. Skip when the `security` lane runs.
- Performance hygiene: obvious repeated work or I/O in a loop. Skip when the `performance` lane runs.
- Testability and tests: changed logic with no test where the codebase tests similar modules, a test that cannot fail, a test that asserts implementation instead of behaviour, missing edge-case tests.
- Accessibility (UI only): missing label or role, focus trap, colour-only meaning, no keyboard path.
- Error handling: swallowed error, wrong status code, retry without limit, partial write with no rollback, an error message that leaks internals, or deviation from the codebase's established error-handling pattern.
