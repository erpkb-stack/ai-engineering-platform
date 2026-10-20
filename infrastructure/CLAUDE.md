# infrastructure

- `docker/` — `python.Dockerfile` (ONE image for all Python services) + `catalog.Dockerfile`, each with its own allow-list `<name>.Dockerfile.dockerignore`; `healthcheck.py` (readiness on `AEOI_SERVICE_PORT`); kafka/postgres init (Phase 21, ADR-033); otel/prometheus/grafana later (Phase 23)
- `docker/wait_for.py` — the initContainer that waits for READY dependencies (compose `depends_on` in Kubernetes, Phase 22a)
- `kubernetes/kind/cluster.yaml` — the kind cluster (Phase 22a, ADR-034). NO hand-written manifests: `scripts/k8s_manifests.py` generates them from `docker compose config` at deploy time (`make k8s-apply`, render only: `make k8s-manifests` -> `.k8s/`). Namespace `ai-engineering-platform`, Pod Security restricted, default-deny policies. kustomize overlays only when a second environment exists
- `observability/` — the OTel collector, Tempo, Loki, Prometheus configs and Grafana provisioning + dashboards (Phase 23a, ADR-035); the stack runs in compose profile `observability` (`make obs-up`). Configs were validated with the real binaries; change them, then `make obs-up` again
- `terraform/` — AWS reference architecture only (EKS, RDS, MSK, ElastiCache, S3, Secrets Manager). **Not applied** in this project; plan-only.
Rules: `.claude/rules/infrastructure.md`. Claude must ask before any `kubectl`, `terraform apply`, or image push.
