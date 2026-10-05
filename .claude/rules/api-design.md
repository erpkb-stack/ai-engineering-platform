---
paths:
  - "services/*/app/api/**"
  - "services/api/**"
---
# API design rules

- Versioned under `/api/v1`. Plural nouns, no verbs except explicit actions (`POST /incidents/{id}/investigate`).
- Long-running work returns `202 Accepted` + `Location` of a status resource; never hold an HTTP request open for an investigation.
- Idempotency: all POSTs that create things accept `Idempotency-Key`; stored in Redis 24h + unique constraint in DB.
- Cursor pagination (`?cursor=&limit=`), never offset for large tables.
- Errors are RFC 7807 problem+json with `correlation_id`.
- OpenAPI is the contract; breaking changes require `/api/v2` or an ADR.
