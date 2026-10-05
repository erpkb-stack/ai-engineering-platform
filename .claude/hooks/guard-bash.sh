#!/usr/bin/env bash
# PreToolUse(Bash): hard-block commands that must never run from an agent session.
# Exit code 2 = block and show stderr to Claude. Settings "deny" rules are the first line;
# this hook catches variants (pipes, sudo, prod contexts) that prefix rules miss.
set -euo pipefail
input="$(cat)"
cmd="$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))')"

block() { echo "BLOCKED by guard-bash.sh: $1" >&2; exit 2; }

case "$cmd" in
  *"--context"*prod*|*"kube-context"*prod*|*"-n production"*) block "production Kubernetes context is human-only (Feature 11/15)";;
  *"sudo "*) block "sudo is not allowed in agent sessions";;
  *"curl "*"| sh"*|*"curl "*"| bash"*|*"wget "*"| sh"*) block "piping remote scripts to a shell";;
  *"DROP DATABASE"*|*"drop database"*|*"TRUNCATE audit_events"*|*"DELETE FROM audit_events"*) block "audit/database destruction";;
  *"cat .env"*|*"cat ./.env"*|*"printenv"*ANTHROPIC*|*"echo \$ANTHROPIC_API_KEY"*) block "reading secrets";;
esac
exit 0
