# AEOI — AI Engineering Operations & Incident Intelligence Platform

An AI engineering command center that investigates production incidents with a LangGraph
multi-agent pipeline, permission-aware RAG, a Tool Gateway, and mandatory human approval —
built on fictional data (**Northwind Cloud Systems**) as a Senior/Staff AI Engineer portfolio project.

> Status: **Phase 1 — architecture.** No implementation code yet. Start with [`architecture.md`](architecture.md).

## Read in this order
1. [`architecture.md`](architecture.md) — requirements, architecture, and the challenges to the original spec
2. [`docs/adr/`](docs/adr/) — decisions ADR-001…ADR-012
3. [`docs/roadmap.md`](docs/roadmap.md) — 31 phases with verify gates
4. [`docs/claude-code-setup.md`](docs/claude-code-setup.md) — how Claude Code is configured for this repo

## Quick start (macOS)
```zsh
make doctor          # checks Homebrew, uv, Python 3.12, Java 21, Node, Docker, kind, Ollama
claude               # open Claude Code in the repo root; type /phase 2 when Phase 1 is approved
```

Disclaimer: this project does not describe or claim any real company's internal systems.
