#!/usr/bin/env bash
# PreToolUse(Edit|Write): protect files whose change requires a deliberate human decision.
set -euo pipefail
input="$(cat)"
path="$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))')"

case "$path" in
  *"/.env"|*"/.env."*|*"/secrets/"*) echo "BLOCKED: secrets files are edited by a human only" >&2; exit 2;;
  *"/alembic/versions/"*)
    # Existing migrations are immutable once merged; new files are fine.
    if [ -f "$path" ] && git -C "$(dirname "$path")" ls-files --error-unmatch "$path" >/dev/null 2>&1; then
      echo "BLOCKED: committed Alembic migrations are immutable — create a new revision instead" >&2; exit 2
    fi;;
  *"/docs/adr/ADR-"*)
    if [ -f "$path" ] && grep -q '^Status: Accepted' "$path"; then
      echo "BLOCKED: Accepted ADRs are immutable — write a new ADR that supersedes it" >&2; exit 2
    fi;;
esac
exit 0
