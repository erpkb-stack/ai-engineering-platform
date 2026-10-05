# infrastructure

- `docker/` — shared Dockerfile bases, compose overrides, otel/prometheus/grafana config (Phase 21/23)
- `kubernetes/` — kustomize base + overlays dev/staging/prod, namespace `ai-engineering-platform` (Phase 22)
- `terraform/` — AWS reference architecture only (EKS, RDS, MSK, ElastiCache, S3, Secrets Manager). **Not applied** in this project; plan-only.
Rules: `.claude/rules/infrastructure.md`. Claude must ask before any `kubectl`, `terraform apply`, or image push.
