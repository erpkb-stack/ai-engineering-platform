# Phase 31 - Interview preparation

Status: **31a BUILT 2026-11-21** (the four docs below, every row checked in the code); mocks
not started. Owner decisions (2026-10-30): scope = map + story bank + measured bullets + live
mocks; target role = **AI Solutions Architect** (decisions, tradeoffs, integration, cost, risk;
AI depth behind it); no 2-minute answer per phase. The first 31a drafts were written in the
Cowork sandbox and lost in a reset; this is the rebuild from the handoff.

## 0. Gate
Phase 30 closed (PR #60, main f2dfcf8). Checked 2026-11-21 before 31a: audit partitions up to
`audit_events_y2027m02` (the keeper runs 3 months ahead); `make demo-clean` removed 0 rows.

## 1. Objective and business reason
The project exists to win an interview. Every claim must survive "show me": a claim the code
contradicts costs more than the claim gains.

## 2. What 31a adds
| file | what |
|---|---|
| `docs/interview/competency-map.md` | question / one-line answer / proof / built-partial-not built, sections A-E; the 8 false claims of the old map corrected at the top |
| `docs/interview/story-bank.md` | 6 true stories (S1-S6), each with evidence files |
| `docs/interview/resume-bullets.md` | 7 bullets, every number with its source; a DO NOT CLAIM list |
| `docs/interview/mock-log.md` | the mock rounds, scored with the interview-coach rubric |

## 3. Found while checking (corrected in the docs)
- Netpol: the latest run is 122 passed / 16 skipped / 0 failed (phase-29.md:88); "130 passed"
  is the Phase 23c/27 count.
- Chaos 4/4 PASS, but all 4 investigations ended PARTIAL (the fake models, phase-28.md): a bullet
  must not imply the investigations finished.
- Rollback proposals in the paid run: 5 correct, 0 wrong, **2 missed**; "0 wrong" alone hides the misses.
- "87 W charger needed" is Apple's spec for the machine, not in any result file: labelled as such.
- Prompt injection: there IS a heuristic flag (`injection_flags`, `aeoi_security.untrusted`);
  the map says partial, not "no detection".
- The copilot exists only as a tool-gateway identity (`agents.yaml`), no agent code.

## 4. Mocks (after 31a is merged)
Rubric: `.claude/agents/interview-coach.md` (1-4 on clarity, correctness, tradeoffs, depth,
honesty; any number without a result file is flagged). One question at a time; the owner answers
without opening docs, ~2 minutes. Panel: hiring manager, principal engineer, security lead.
Rounds: 1 decisions, 2 AI quality, 3 failure/security. Log in `docs/interview/mock-log.md`.

## 5. Verify
- [ ] the map has no claim the code contradicts (reviewer: re-check the "Corrected" table and every "built" row)
- [ ] every bullet number cites a file
- [ ] 3 mock rounds logged (decisions, AI quality, failure/security); the weakest answer redone to >= 3 on every axis
- [ ] the owner confirms the bullets to use
Then: roadmap Phase 31 [x], CLAUDE.md.
