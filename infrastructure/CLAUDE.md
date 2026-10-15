# infrastructure

- `docker/` — `python.Dockerfile` (ONE image for all Python services) + `catalog.Dockerfile`, each with its own allow-list `<name>.Dockerfile.dockerignore`; `healthcheck.py` (readiness on `AEOI_SERVICE_PORT`); kafka/postgres init (Phase 21, ADR-033); otel/prometheus/grafana later (Phase 23)
- `kubernetes/` — kustomize base + overlays dev/staging/prod, namespace `ai-engineering-platform` (Phase 22)
- `terraform/` — AWS reference architecture only (EKS, RDS, MSK, ElastiCache, S3, Secrets Manager). **Not applied** in this project; plan-only.
Rules: `.claude/rules/infrastructure.md`. Claude must ask before any `kubectl`, `terraform apply`, or image push.
