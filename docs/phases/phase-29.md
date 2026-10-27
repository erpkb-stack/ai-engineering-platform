# Phase 29 - End-to-end demonstration (ADR-043)

Status: **29a BUILT 2026-10-27** (sandbox rehearsal only). Owner (2026-10-27): "Phase 29";
clean = **fresh containers, keep data**; models = **fake to rehearse, real for the 3 passes**;
the 4 missing steps = **build all four**.

## 0. Gate
- Phase 28 closed: PRs #43-#45 merged, main 43c4d0b green, `git ls-files` of 0029 + ADR-042 +
  the restored 28c-1 result = 3.

## 1. Need check - the Phase 1 script against what exists
| min | step | exists? | how 29a checks it |
|---|---|---|---|
| 1:00 | alert webhook -> incident | **no** (only POST /incidents) | 29a: POST /incidents; 29b builds the webhook |
| 2:00 | orchestrator plan | yes | the investigation ends named (COMPLETE on real models) |
| 3:00 | parallel agents | yes | the 4 evidence agents' intervals all overlap |
| 5:00 | evidence | yes | kinds LOG METRIC DEPLOY CONFIG INCIDENT |
| 6:00-6:30 | hypothesis + critic challenge | yes (real models) | #1 = DEPLOY-4821, VALIDATED, critic "supported" |
| 6:30 | decoy rejected | **differs** | the system never proposes the cart-cache restart (no support in code) - checked as "never #1 / HIGH"; the script line is corrected |
| 7:30 | removed sentence shown as a diff | **no** (counts only) | 29c |
| 8:00 | evidence graph | **no** (edges in the API only) | 29d |
| 9:00 | report | yes | postmortem root cause = DEPLOY-4821, similar-incident section |
| 10:00 | approval -> controlled tool | yes (real models) | the investigation's proposal; ENGINEER 403; IC approves -> EXECUTED, MITIGATED |
| 11:00 | audit trail | yes | decision record: who, when, why, args sha256, tool call |
| 11:30 | injection ignored | yes | the adversarial document is never returned |
| 12:00 | MTTI manual vs AI | **no** | 29e - needs the owner's timed manual runs |

Found: **the rollback changes the demo data** (a new deploy row on checkout-api) -> `make
demo-clean` after every pass. **`make db-seed` truncates rag + history** -> never before a demo.

## 2. What 29a adds
`tests/demo/demolib.py` (checks, `tests/unit/test_demo.py`), `tests/demo/run.py`, `make demo`,
`make demo-clean`, `make demo-fresh`; results in `data/demo/results/`.

Sandbox rehearsal (processes, fake models, no Java catalog): 9 PASS, 4 n/a, 1 FAIL
(`services_ready`: no catalog in the sandbox); investigation PARTIAL in 38 s, the 4 agents
overlapped 0.7 s, evidence COMMIT CONFIG DEPLOY DOC INCIDENT LOG METRIC RUNBOOK.

## 3. Next
29b webhook (signed, auto-triage, services from the catalog; security tests + threat-model row),
29c removed-sentence diff, 29d evidence-graph screen, 29e MTTI. Then the gate: `make
demo-fresh && make demo && make demo-clean`, 3 real-model PASS in a row.

## 4. Verify
- [ ] 29a rehearsal on the Mac (fake): REHEARSAL-OK, `make demo-clean` removes 0 rows.
- [ ] 29b-29e built and in the demo.
- [ ] 3 real-model PASS in a row, each after `make demo-fresh`, files in `data/demo/results/`.
