# Lane checklist: robustness

Read and apply this checklist strictly to the lines added, modified, or removed in `diff.patch` (inspect surrounding callers and transaction boundaries solely as read-only context to verify impact). Report only robustness defects introduced or worsened by this PR. Map each item to a `dimension` value.

**Codebase and framework awareness:** Inspect how the existing codebase and its framework manage transactions, concurrency, retries, queues, and resource lifecycles. Follow any convention files or framework patterns that the codebase points to.

**Robustness**
- Concurrency: a race on shared state, check-then-act without a lock or constraint, a missing idempotency key on a retried operation.
- Transactions and side effects: an external call, message or email sent before the commit, writes that can half-complete, no compensation on failure.
- Async: an unawaited task, unbounded retries, no timeout, no dead-letter path, ordering assumptions on a queue.
- Resource use: a leaked connection, file or lock; no cleanup on the error path.
- Complexity: control flow that is hard to follow and hides one of the failures above.
