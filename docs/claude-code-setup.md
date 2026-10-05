# How Claude Code is configured for AEOI

This file lists every Claude Code configuration file in the repo, what it does, and **why it is
there**. If a file has no clear job, it should be deleted.

## 1. Instruction files (context Claude loads)

| File | Loads | Why it exists |
|---|---|---|
| `AGENTS.md` | Imported by `CLAUDE.md` (`@AGENTS.md`) | Rules that are the same for every AI tool (Codex, Cursor, Copilot). One source of truth. |
| `CLAUDE.md` | Every session | Claude-specific info: current phase, mentor mode, machine limits, things Claude must never do. Kept under 150 lines. |
| `CLAUDE.local.md` (copy from `.example`) | Every session, gitignored | Your personal notes: Docker RAM, which Ollama models you pulled, what phase you are in. |
| `services/*/CLAUDE.md`, `frontend/`, `infrastructure/`, `tests/`, `libs/` | Only when Claude opens files in that folder | Each service's purpose, owned schema, events, and what it must never do. This keeps the root file small. |
| `.claude/rules/security.md` | Every session (no `paths:`) | Security rules apply everywhere. |
| `.claude/rules/{python,java,langgraph-agents,database,api-design,frontend,testing,infrastructure}.md` | Only for files that match their `paths:` glob | Rules for one language or layer cost no context when Claude works on other files. |

**Important:** because `CLAUDE.md` exists, Claude Code does **not** read `AGENTS.md` on its own.
That is why `CLAUDE.md` starts with `@AGENTS.md`.

## 2. Skills (`.claude/skills/<name>/SKILL.md`) — run with `/name` or picked automatically

| Skill | Use |
|---|---|
| `/phase N` | Runs the 15-step phase contract. The gate stops it if the previous phase isn't verified. Only you can start it (`disable-model-invocation`). |
| `/adr` | Writes or supersedes an ADR. |
| `/new-service` | Standard FastAPI service scaffold (has a supporting `layout.md`). |
| `/new-agent` | LangGraph agent with a versioned prompt, schemas, budgets and tests. Makes Claude justify why it is an agent and not a function. |
| `/new-tool` | Tool Gateway contract: schemas, authorization, side-effect class, audit, security tests. |
| `/db-migration` | Safe Alembic migration (expand → migrate → contract, `CONCURRENTLY`, index comments). |
| `/eval-run` | Compares prompt/model versions on a dataset. Uses real numbers only, with confidence intervals. |
| `/security-test` | RBAC, retrieval leakage, prompt-injection and exfiltration tests. |
| `/interview-prep` | Turns a component into answers in the 6-part interview format. |
| `/synthetic-data` | Fictional "Northwind Cloud Systems" data with fixed seeds and planted ground truth. |
| `/local-dev-macos` | Intel-Mac commands, compose profiles, troubleshooting. |

## 3. Subagents (`.claude/agents/*.md`) — separate context window and limited tools

| Agent | Model | Tools | Why it is separate |
|---|---|---|---|
| `architecture-critic` | opus | read-only, plus project memory | Reviews without bias from the code it just wrote. Remembers recurring problems. |
| `security-reviewer` | sonnet | read + Bash (for `git diff`) | Focused security review; cannot edit files. |
| `test-engineer` | sonnet | full | Writes and runs tests. Must not weaken an assertion to make a test pass. |
| `langgraph-engineer` | opus | full + WebFetch | Checks LangGraph APIs against the current docs, because the APIs change often. |
| `interview-coach` | opus | read-only | Mock interviewer that scores your answers and flags numbers you can't back up. |

## 4. Other configuration

| File | Purpose |
|---|---|
| `.claude/settings.json` | Team permissions: what is **allowed** without asking (lint/test), what Claude must **ask** about (compose up, alembic, kubectl, git push), and what is **denied** (reading `.env`/secrets, force push, prune). Also sets env vars and hooks. |
| `.claude/hooks/guard-bash.sh` | PreToolUse: blocks sudo, production kube contexts, `curl \| sh`, audit-table deletes, and secret reads, including variants that prefix rules miss. |
| `.claude/hooks/protect-files.sh` | PreToolUse: blocks edits to secrets, to merged Alembic migrations, and to Accepted ADRs. |
| `.claude/hooks/format-on-save.sh` | PostToolUse: runs ruff/prettier on each file Claude edits. |
| `.claude/hooks/session-context.sh` | SessionStart: puts the current phase, next roadmap item and git status into context. |
| `.claude/output-styles/staff-mentor.md` | Optional style (`/config` → Output style): ruthless mentor plus an interview box after each change. |
| `.claude/commands/phase-status.md` | `/phase-status`. One legacy-format command, kept as an example. New workflows should be skills. |
| `.mcp.json` | Project MCP servers: read-only Postgres (Phase 3+), Playwright (Phase 17 UI checks), GitHub. Claude asks you to approve project servers the first time. |
| `.github/workflows/claude.yml` | Claude Code GitHub Action: write `@claude` in a PR comment to get a review or fix. CI/CD itself comes in Phase 24. |
| Auto memory (`~/.claude/projects/<repo>/memory/MEMORY.md`) | Written by Claude itself. Not in git. Check it with `/memory`. |
| `CLAUDE.local.md`, `.claude/settings.local.json` | Personal overrides, gitignored. |

## 5. Things to check yourself (don't assume)
- Run `/context` once to confirm which memory files and rules loaded.
- `@modelcontextprotocol/server-postgres` is the reference server and is no longer actively maintained.
  Use it for read-only local work only, or swap in a maintained Postgres MCP before relying on it.
  It connects as a read-only role `aeoi_readonly`, which is created in Phase 3.
- The GitHub MCP entry needs `GITHUB_PERSONAL_ACCESS_TOKEN` in your shell environment.
  Use a fine-grained, read-mostly token.
- Hooks run on your Mac with your user permissions. Read them before you trust them.
