# Lane checklist: security

**Scope by `LEVEL`:** At `LEVEL: PR`, read and apply this checklist strictly to lines added, modified, or removed in `lanes/security.patch` (inspect callers, guards, and middleware solely as read-only context to verify impact; report only security flaws introduced or worsened by this PR). At `LEVEL: CODEBASE`, read and apply this checklist exhaustively across every file in your assigned `<files_list>` at `head_sha` (pre-existing vulnerabilities in scope are valid; set `introduced_by_pr: "codebase"`). Map each item to a `dimension` value.

**Codebase and framework awareness:** Inspect how the existing codebase and its framework enforce authentication, authorization, tenant isolation, validation, and secret handling. Follow any security checklists, middleware definitions, or framework convention files that the codebase points to.

**Security** (dimensions: STRIDE category or OWASP Top 10 category)
- Authentication and authorisation: a new or changed entry point with no check, a check on the wrong object, trust in a client-controlled id, header or role, tenant or owner scope missing from a query, or bypassing the codebase's standard auth guard/middleware.
- Input: injection (SQL, command, template, path, header), unsafe deserialisation, missing size or type limits, SSRF through a user-supplied URL, open redirect.
- Data: personal or secret data in logs, errors, URLs or responses; missing encryption where the repo requires it; over-broad response fields.
- Secrets and config: a secret in the diff, a weakened security setting, debug left on, permissive CORS.
- Dependencies: a new dependency with no reason, a version downgrade, an unpinned version, a changed registry or integrity hash.
- Rate and abuse: a new expensive or enumerable endpoint with no limit.
