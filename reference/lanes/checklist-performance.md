# Lane checklist: performance

Read and apply this checklist to the changed code and the code it reaches. Report only what the diff adds or changes. Map each item to a `dimension` value.

**Codebase and framework awareness:** Inspect how the existing codebase and its framework handle data access, batching, pagination, caching, and background jobs. Follow any framework or architecture convention files that the codebase leads to before flagging a pattern.

**Performance** (static: read the code, run nothing)
- Data access in a loop (N+1), a missing batch or join, an unbounded query, a list endpoint with no pagination.
- Repeated network or disk I/O in a loop, missing timeout, missing cache where the repo caches the same call elsewhere.
- Heavy work on a request path that belongs in a background job per the codebase's job pattern.
- Large in-memory collections, needless copies, quadratic loops on unbounded input.
- Front end: re-renders on every change, large bundles added, blocking work on the main thread.
