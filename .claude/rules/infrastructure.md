---
paths:
  - "infrastructure/**"
  - "**/Dockerfile"
  - "docker-compose*.yml"
  - ".github/workflows/**"
---
# Infrastructure rules

- Dockerfiles: multi-stage, pinned base digests in CI, non-root UID 10001, HEALTHCHECK, no secrets in layers, `.dockerignore` per service.
- Images must build for linux/amd64 (owner's Intel Mac) and linux/arm64 (CI matrix, `docker buildx`).
- Compose: profiles `infra | core | agents | observability | full`; resource limits on every service; secrets via `secrets:`.
- K8s: namespace `ai-engineering-platform`; every Deployment has requests/limits, readiness + liveness probes, PDB for >1 replica; agent workers scale via HPA (CPU) → KEDA on Kafka lag in production.
- GitHub Actions: least-privilege `permissions:`, OIDC to cloud (no long-lived keys), environments dev/staging/prod with required reviewers on prod.
