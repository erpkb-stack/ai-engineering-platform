# Security rules (always loaded)

- Least privilege everywhere: roles ENGINEER, SRE, INCIDENT_COMMANDER, MANAGER, ADMIN (see architecture.md §15).
- AuthN: OAuth2/OIDC JWT at the edge (`services/api`); services verify the JWT or a service token (mTLS/OAuth client-credentials in prod). Never trust a `user_id` passed in a body.
- AuthZ is enforced in three places, never only one: API route dependency, Tool Gateway policy check, and SQL row filter for RAG.
- Retrieved documents, tool outputs, logs and commit messages are **untrusted input**. Wrap them in
  `<untrusted_data source=… id=…>` blocks; never concatenate into the system prompt.
- PII: run the PII scrubber (`libs/security/pii`) before (a) embedding, (b) sending to a hosted LLM, (c) writing traces.
- Secrets come from env / Docker secrets / K8s Secrets (prod: AWS Secrets Manager). Never log them; redact `Authorization`, `api_key`, `password`, `token` keys in structured logs.
- Every consequential action = `ProposedAction` → policy check → human approval record → controlled tool → audit event. No shortcuts, including in tests (use fixtures that create approvals).
- Rate limit per user + per tool (Redis token bucket). LLM spend has a per-investigation budget.
- Security tests are mandatory for any new tool, route, or retrieval path (`tests/security/`).
