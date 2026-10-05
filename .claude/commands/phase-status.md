---
description: Show AEOI phase progress, current blockers, and the next action (legacy command example — skills are preferred for new workflows).
---
Read `docs/roadmap.md` and `CLAUDE.md`. Output:
1. A one-line progress bar of phases done / 31.
2. The current phase, its Verify checklist, and which items are unchecked.
3. Any failing tests or TODOs in files changed since the last tag (`git log --oneline $(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)..HEAD`).
4. The single next action.
