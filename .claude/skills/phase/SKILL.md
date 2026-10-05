---
name: phase
description: Run one AEOI build phase end-to-end using the mandatory 15-step contract (objective → code → tests → verify → interview answers). Use when the user says "start phase N", "continue phase", or "next phase".
argument-hint: "<phase-number>"
disable-model-invocation: true
---
# Run Phase $ARGUMENTS

## 0. Gate
1. Open `docs/roadmap.md`. If the previous phase's **Verify** box is unchecked, STOP and tell the user what is unverified.
2. Read the matching section(s) of `architecture.md` and the ADRs it cites.
3. Challenge the phase scope first (mentor mode): list anything in the phase that is unnecessary or premature, and propose the cut. Ask before cutting.

## 1–15. The contract (produce every item, in this order)
1. Objective
2. Business reason (which KPI from architecture.md §4 it moves)
3. Architecture (diagram if >3 components; mark Prototype / Production / Enterprise-scale)
4. Files to create/change (tree)
5. Complete code — no `...`, no TODO placeholders in delivered files
6. Exact macOS/zsh commands (Intel Mac: assume x86_64, Docker Desktop)
7. Configuration (`.env.example` keys, compose profile, settings)
8. Tests (unit + the relevant security/failure tests from `.claude/rules/testing.md`)
9. How to run
10. How to verify — concrete commands with expected output shape
11. Failure scenarios (what breaks, how it shows up, how it recovers)
12. Production considerations
13. Interview questions (≥5)
14. 30-second answers
15. 2-minute answers → append to `docs/interview/phase-XX.md` using the `interview-prep` skill

## Done
- Run lint + type check + tests. If anything fails: diagnose root cause → explain → fix → add regression test → note prevention. Never hide a failure.
- Tick the phase in `docs/roadmap.md` only after the user confirms it runs on their Mac.
- Update `CLAUDE.md` "Current phase" line.
