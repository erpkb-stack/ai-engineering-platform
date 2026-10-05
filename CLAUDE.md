@AGENTS.md

# CLAUDE.md — Claude Code project memory for AEOI

<!-- Keep this file under ~150 lines. Topic rules live in .claude/rules/ (path-scoped),
     procedures live in .claude/skills/, specialist reviewers in .claude/agents/.
     HTML comments like this one are stripped before Claude sees the file. -->

## Current phase
**Phase 2 — Repository & dev environment** (Phase 1 done; ADRs still `Proposed`).
Phase status is tracked in `docs/roadmap.md`. Do NOT start a phase until the previous
phase's "Verify" checklist passes. Use the `/phase` skill to run a phase.

## How to work in this repo
- Before writing code for a phase, read the relevant section of `architecture.md` and any ADR it cites.
- Every phase follows the 15-step contract in `.claude/skills/phase/SKILL.md`.
- If a design decision changes, write/update an ADR with the `/adr` skill **in the same change**.
- Prefer the simplest design that meets the requirement. If the spec demands something
  heavier, implement it but record the cost in the ADR "Tradeoffs" section.
- Distinguish **Prototype / Production / Enterprise-scale** in every design note.
- When unsure whether something is a fact or a hypothesis — it is a hypothesis.

## Mentor mode (the owner asked for this explicitly)
Act as a ruthless Staff-level reviewer. Challenge unnecessary services, agents, frameworks,
weak data models, bad API design, poor observability and unjustified tech. Say "this is
unnecessary" when it is. Never flatter. Use the `architecture-critic` subagent for big reviews.

## Stack (pinned decisions — see ADRs)
- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x async + Alembic, `uv` for envs
- LangGraph for orchestration (ADR-001); PostgreSQL 16 + pgvector (ADR-002)
- Kafka (KRaft, single broker locally) (ADR-003); Redis 7
- Java 21 + Spring Boot 3 for `service-catalog` (ADR-005)
- React + TypeScript + Vite for `frontend/`
- LLMs: Anthropic Claude for reasoning agents; Ollama local model for cheap tasks / offline dev;
  local embedding model by default. Provider is configuration, never hard-coded (Feature 18).
- OpenTelemetry → Prometheus/Grafana (ADR-010); Docker Compose locally; kind for local K8s

## Machine constraints (owner's Mac)
- Intel x86_64 macOS. No Apple-Silicon GPU → Ollama runs CPU-only: use ≤3–4B models locally.
- Docker Desktop memory is the main bottleneck. Use compose **profiles**
  (`infra`, `core`, `agents`, `observability`, `full`) — never require all 11 services + infra
  just to run one test.

## Commands
- `make doctor` — prerequisites · `make setup` — one-time env, hooks, .env, secrets
- `make check` — lint + mypy --strict + unit tests (must pass before any commit)
- `make up [PROFILE=infra|kafka]` · `make verify-infra` · `make down` · `make logs SVC=x`
- Python: `uv` workspace — add a dep with `uv add --package aeoi-<lib> <dep>`; commit `uv.lock`.
- Work on a branch: the `no-commit-to-branch` hook blocks commits to `main`.

## Things Claude must never do here
- Put secrets in code, compose files, or committed `.env` — use `.env.local` (gitignored) / Docker secrets.
- Call an LLM SDK directly from an agent — always via the LLM Gateway client.
- Give an agent a raw DB/HTTP client to an "external" system — always via Tool Gateway.
- Invent evaluation/latency/cost numbers for docs, README or resume bullets.
- Run `kubectl`, `docker push`, or `git push` without the owner asking.

## Where things are
- Architecture: `architecture.md` · ADRs: `docs/adr/` · Roadmap: `docs/roadmap.md`
- Interview prep: `docs/interview/` · Demo script: `docs/demo/demo-scenario.md`
- Per-area instructions: nested `CLAUDE.md` in each `services/*`, `frontend/`, `infrastructure/`, `tests/`
