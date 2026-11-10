# Lane checklist: security

Read and apply this checklist to the changed code and the code it reaches. Report only what the diff adds or changes. Map each item to a `dimension` value.

**Codebase and framework awareness:** Inspect how the existing codebase and its framework enforce authentication, authorization, tenant isolation, validation, and secret handling. Follow any security checklists, middleware definitions, or framework convention files that the codebase points to.

**Security** (dimensions: STRIDE category or OWASP Top 10 category)
- Authentication and authorisation: a new or changed entry point with no check, a check on the wrong object, trust in a client-controlled id, header or role, tenant or owner scope missing from a query, or bypassing the codebase's standard auth guard/middleware.
- Input: injection (SQL, command, template, path, header), unsafe deserialisation, missing size or type limits, SSRF through a user-supplied URL, open redirect.
- Data: personal or secret data in logs, errors, URLs or responses; missing encryption where the repo requires it; over-broad response fields.
- Secrets and config: a secret in the diff, a weakened security setting, debug left on, permissive CORS.
- Dependencies: a new dependency with no reason, a version downgrade, an unpinned version, a changed registry or integrity hash.
- Rate and abuse: a new expensive or enumerable endpoint with no limit.
