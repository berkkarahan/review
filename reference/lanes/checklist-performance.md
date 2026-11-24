# Lane checklist: performance

**Scope by `LEVEL`:** At `LEVEL: PR`, read and apply this checklist strictly to lines added, modified, or removed in `lanes/performance.patch` (inspect surrounding code solely as read-only context to verify impact; report only performance issues introduced or worsened by this PR). At `LEVEL: CODEBASE`, read and apply this checklist exhaustively across every file in your assigned `<files_list>` at `head_sha` (pre-existing performance bottlenecks in scope are valid; set `introduced_by_pr: "codebase"`). Map each item to a `dimension` value.

**Codebase and framework awareness:** Inspect how the existing codebase and its framework handle data access, batching, pagination, caching, and background jobs. Follow any framework or architecture convention files that the codebase leads to before flagging a pattern.

**Performance** (static: read the code, run nothing)
- Data access in a loop (N+1), a missing batch or join, an unbounded query, a list endpoint with no pagination.
- Repeated network or disk I/O in a loop, missing timeout, missing cache where the repo caches the same call elsewhere.
- Heavy work on a request path that belongs in a background job per the codebase's job pattern.
- Large in-memory collections, needless copies, quadratic loops on unbounded input.
- Front end: re-renders on every change, large bundles added, blocking work on the main thread.
