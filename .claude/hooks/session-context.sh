#!/usr/bin/env bash
# SessionStart: print the current phase + git state; stdout is added to Claude's context.
cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || exit 0
echo "== AEOI session context =="
grep -m1 -E '^\*\*Phase [0-9]+' CLAUDE.md 2>/dev/null || true
if [ -f docs/roadmap.md ]; then
  echo "Next unchecked roadmap item:"; grep -m1 -E '^\- \[ \]' docs/roadmap.md || echo "(none)"
fi
git rev-parse --is-inside-work-tree >/dev/null 2>&1 && { echo "Branch: $(git branch --show-current)"; git status --short | head -15; }
exit 0
