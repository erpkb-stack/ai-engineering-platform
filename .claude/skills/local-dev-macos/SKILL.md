---
name: local-dev-macos
description: Exact macOS/zsh commands to install prerequisites, start/stop infrastructure, run services, view logs, and clean up for AEOI on an Intel Mac with Docker Desktop. Use when the user asks how to run anything locally or something fails to start.
---
# Local development on macOS (Intel x86_64)

## Prerequisites (once)
```zsh
brew install uv python@3.12 openjdk@21 node@22 kind kubectl helm jq
brew install --cask docker            # Docker Desktop → Settings → Resources: ≥ 10 GB RAM, 6 CPUs
brew install ollama && ollama pull llama3.2:3b && ollama pull nomic-embed-text
make doctor
```
## Compose profiles (pick the smallest that does the job)
| Profile | Contains | Approx RAM |
|---|---|---|
| infra | postgres+pgvector, redis, kafka(KRaft) | ~2 GB |
| core | infra + api, incident, llm-gateway, tool-gateway, rag | ~4 GB |
| agents | core + orchestrator, agents (1 replica) | ~5.5 GB |
| observability | otel-collector, prometheus, grafana | ~1 GB |
| full | everything incl. service-catalog (JVM) + frontend | ~9–10 GB |
Commands are added in Phase 2/21 (`make up PROFILE=core`, `make logs SVC=api`, `make down`, `make clean`).
## Troubleshooting order
1. `docker compose ps` → which container is unhealthy? 2. `docker compose logs <svc> --tail 100`
3. Port clash: `lsof -nP -iTCP:<port> -sTCP:LISTEN` 4. Out of memory: Docker Desktop RAM / use smaller profile.
5. Ollama slow on Intel: expected (CPU only) — route heavy agents to Claude via `LLM_ROUTING` config.
