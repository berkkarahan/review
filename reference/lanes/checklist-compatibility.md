# Lane checklist: compatibility

Read and apply this checklist to the changed code and the code it reaches. Report only what the diff adds or changes. Map each item to a `dimension` value.

**Codebase and framework awareness:** Check how the existing codebase and its framework version APIs, schemas, migrations, and feature flags. Follow any schema, migration, or contract convention files that the codebase points to.

**Compatibility**
- Public contract: a removed or renamed field, endpoint, route, event or message field; a new required input; a changed type, default or error shape. Name the consumers when the repo or linked docs show them.
- Each breaking change needs a migration: versioning, a deprecation path, or a coordinated rollout matching the codebase's rollout conventions. Fill `breaking.consumers` and `breaking.migration`.
- Data migrations: safe to run while old and new code both run, reversible or with a stated rollback, no long lock on a large table, no data loss.
- Config and feature flags: a new required setting with no default, a flag removed while code still reads it.
