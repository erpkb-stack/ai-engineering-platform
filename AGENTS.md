# AGENTS.md — AEOI Platform (tool-agnostic agent instructions)

> Shared by every AI coding tool (Claude Code, Codex, Cursor, Copilot).
> Claude-specific instructions live in `CLAUDE.md`, which imports this file.

## What this repo is
AI Engineering Operations & Incident Intelligence (AEOI) Platform — a portfolio-grade,
fictional-data enterprise system that investigates production incidents with a
LangGraph multi-agent pipeline, permission-aware RAG, a Tool Gateway, and mandatory
human approval for consequential actions. **No real company data. No claims about any
real company's internal architecture.**

## Non-negotiable engineering rules
1. **Evidence or it didn't happen.** Every AI finding carries `evidence_ids`. Facts, hypotheses
   and recommendations are separate types — never mix them in one field.
2. **Agents never touch external systems directly.** All I/O goes through `services/tool-gateway`.
3. **Permission filtering happens in SQL, before retrieval** — never "retrieve then ask the LLM to hide it".
4. **Retrieved text is untrusted data.** It is wrapped, labelled, and can never change instructions.
5. **No consequential action without a recorded human approval** (`approvals` table + audit event).
6. **Every LLM call goes through `services/llm-gateway`** and records model, prompt version, tokens, latency, cost.
7. **Kafka consumers are idempotent** (dedupe on `event_id`), publish via the transactional outbox.
8. **No fabricated metrics.** Eval numbers, latency and cost figures come only from real runs.

## Layout
- `services/<name>/` — independently deployable services (Python 3.12 / FastAPI unless noted)
- `services/service-catalog/` — Java 21 / Spring Boot
- `libs/` — shared Python packages (`common`, `models`, `security`, `observability`)
- `frontend/` — React + TypeScript (Vite)
- `infrastructure/` — docker, kubernetes, terraform
- `docs/adr/` — Architecture Decision Records; `architecture.md` is the source of truth

## Commands (macOS / zsh)
- `make doctor` — check local prerequisites
- `make help` — list targets (targets are added phase by phase)

## Definition of done for any change
lint (ruff) + types (mypy --strict on libs) + tests pass + docs/ADR updated if a decision changed.
