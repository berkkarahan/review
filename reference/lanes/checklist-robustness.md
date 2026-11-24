# Lane checklist: robustness

**Scope by `LEVEL`:** At `LEVEL: PR`, read and apply this checklist strictly to lines added, modified, or removed in `lanes/robustness.patch` (inspect surrounding callers and transaction boundaries solely as read-only context to verify impact; report only robustness defects introduced or worsened by this PR). At `LEVEL: CODEBASE`, read and apply this checklist exhaustively across every file in your assigned `<files_list>` at `head_sha` (pre-existing concurrency, transaction, and resource-leak defects in scope are valid; set `introduced_by_pr: "codebase"`). Map each item to a `dimension` value.

**Codebase and framework awareness:** Inspect how the existing codebase and its framework manage transactions, concurrency, retries, queues, and resource lifecycles. Follow any convention files or framework patterns that the codebase points to.

**Robustness**
- Concurrency: a race on shared state, check-then-act without a lock or constraint, a missing idempotency key on a retried operation.
- Transactions and side effects: an external call, message or email sent before the commit, writes that can half-complete, no compensation on failure.
- Async: an unawaited task, unbounded retries, no timeout, no dead-letter path, ordering assumptions on a queue.
- Resource use: a leaked connection, file or lock; no cleanup on the error path.
- Complexity: control flow that is hard to follow and hides one of the failures above.
