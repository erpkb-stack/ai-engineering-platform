#!/usr/bin/env bash
# PostToolUse(Edit|Write): auto-format the touched file. Never fails the tool call.
input="$(cat)"
path="$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))')"
[ -f "$path" ] || exit 0
case "$path" in
  *.py)   command -v uv >/dev/null && (uv run --quiet ruff format "$path" && uv run --quiet ruff check --fix --quiet "$path") >/dev/null 2>&1 ;;
  *.ts|*.tsx|*.js|*.json|*.css) [ -x "$CLAUDE_PROJECT_DIR/frontend/node_modules/.bin/prettier" ] && "$CLAUDE_PROJECT_DIR/frontend/node_modules/.bin/prettier" --write "$path" >/dev/null 2>&1 ;;
  *.java) : ;; # Spotless runs in Maven build (Phase 20)
esac
exit 0
