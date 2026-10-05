#!/usr/bin/env zsh
# Prerequisite check for AEOI on macOS. Read-only; installs nothing.
ok()   { print -P -- "%F{green}✔%f ${1//\%/%%}"; }
bad()  { print -P -- "%F{red}✘%f ${1//\%/%%}"; FAIL=1; }
warn() { print -P -- "%F{yellow}!%f ${1//\%/%%}"; }
FAIL=0
print "Arch: $(uname -m)  macOS: $(sw_vers -productVersion 2>/dev/null)"
[[ "$(uname -m)" == "x86_64" ]] && warn "Intel Mac: Ollama is CPU-only — use ≤3–4B models; route reasoning agents to Claude."
mem_gb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 )); print "RAM: ${mem_gb} GB"
(( mem_gb < 16 )) && warn "<16 GB RAM: run 'core' or 'agents' compose profiles, not 'full'."
command -v brew   >/dev/null && ok "Homebrew"      || bad "Homebrew  → https://brew.sh"
command -v uv     >/dev/null && ok "uv $(uv --version | awk '{print $2}')" || bad "uv → brew install uv"
if command -v python3.12 >/dev/null || uv python find 3.12 >/dev/null 2>&1; then ok "Python 3.12"; else bad "Python 3.12 → uv python install 3.12"; fi
if /usr/libexec/java_home -v 21 >/dev/null 2>&1; then ok "Java 21"; else bad "Java 21 → brew install openjdk@21"; fi
command -v node   >/dev/null && ok "Node $(node -v)" || bad "Node LTS → brew install node@22"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  ok "Docker running ($(docker info --format '{{.MemTotal}}' | awk '{printf "%.1f GB allocated", $1/1024/1024/1024}'))"
else bad "Docker Desktop not running → brew install --cask docker, then open it"; fi
command -v kind    >/dev/null && ok "kind"    || warn "kind (needed Phase 22) → brew install kind"
command -v kubectl >/dev/null && ok "kubectl" || warn "kubectl (needed Phase 22) → brew install kubectl"
command -v ollama  >/dev/null && ok "Ollama"  || warn "Ollama (optional local LLM) → brew install ollama"
command -v claude  >/dev/null && ok "Claude Code CLI" || warn "Claude Code CLI → https://code.claude.com/docs"
[[ -n "$ANTHROPIC_API_KEY" ]] && ok "ANTHROPIC_API_KEY set" || warn "ANTHROPIC_API_KEY not set (needed Phase 5)"
exit $FAIL
