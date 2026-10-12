# Phase 15 — Release risk analysis

## 0. Gate and scope challenge
- **Gate: passed.** PR #15 merged (9433be9); Phase 14 verified on the Mac (`make code-smoke`
  21/21, regressions ✔, integration 223 passed).
- **Challenge 1 — "how good is it?"** Synthetic deploys are not linked to incidents: there is
  no ground truth. Owner decision: **rubric band + labelled cases**, no accuracy claim. What we
  CAN measure is selectivity: how often would it flag a release (`make risk-scan`).
- **Challenge 2 — "unknown is not safe"** sounded right, but the first scan showed 102 of 104
  flagged releases were flagged only because a part could not be checked. Kept the rule, but a
  report now says `findings_band` and `coverage` apart from the band.
- **Challenge 3 — limit changes.** As MEDIUM they would flag 40.8 % of releases. Context only.
- **Draft notice.** An interrupted earlier attempt had left an unreviewed Phase 15 draft in the
  sandbox (15 changed + 8 new files on top of main). It was diffed against main, read in full,
  completed (range target, coverage, tests, docs) and independently reviewed before shipping.

## 1. Objective
Before a PR or release candidate ships - or for a deploy, after the fact - say how risky it is,
why (each reason cites evidence), and how it would be taken back out. Advisory only.

## 2. Business reason
The Phase 14 demo found the N+1 only AFTER the incident. The same rule on PR #812 says HIGH
before it ships. And the way back is concrete: "a redeploy; flipping order_batching back would
NOT undo the code" - the mistake the critic and the postmortem made on the Mac.

## 3. Architecture
```
api POST /api/v1/release-risk ─► orchestrator /v1/release-risk ─► agents release_risk (caller's token)
   target (PR | range | deploy) ─► compare_commits ─► Phase 14 rules + flag guard
   query_service_catalog ─► find_references (removed field) ─► rubric ─► release_assessments
```
| Guarantee | Enforced by | Test (mutation-checked) |
|---|---|---|
| Advisory: nothing gates a deploy on it | no reader exists; `advisory: true` | e2e + smoke |
| code:read + deploys:read for every read and write | api `guard`, orchestrator `_readable` | api 403 + e2e MANAGER |
| Caller's own token only; investigation-delegated tokens refused | agents `_user_token(None, None)` | e2e delegated token 403 |
| Idempotency-Key per requester | unique (requester, key) + lookup by requester | e2e (lookup without requester → fails) |
| Every factor cites returned evidence | cited-first cap + filter | unit + e2e |
| Capped references are "not checked", never "nobody" | `_impact` | unit (cap check removed → fails) |
| Flag way back only for ONE positive flag check | `positive_flag_check` | unit (old matcher → 4 fail) |
| Flag "read" = identifier or string literal in a source file | `names_and_strings` | unit |
| Not checked raises to MEDIUM, never HIGH | `band_of` | unit (old rule → fails) |
| Labelled cases hold; limit changes stay context | `make risk-scan` (exit 1 on a miss) | limit mutation → 3 misses |
| Swapped range = FAILED, slow tool = PARTIAL report (not 503) | agent | unit + e2e |

## 4. Files
```
docs/adr/ADR-025-release-risk.md (+ README row) · docs/phases/phase-15.md · docs/interview/phase-15-release-risk.md
libs/codeparse    flag_guard + positive_flag_check, flag_variants, names_and_strings, Finding.guard
libs/models       api/release.py (new), api/agents.py (AgentRunResult.release)
libs/db           models/orchestrator.py ReleaseAssessment; alembic 0023_release_assessments
services/agents   release/{agent,rubric,scan}.py (new); code/agent.py scan_commit (shared); api, config, main
services/orchestrator  release.py (new), main, clients, config (release_timeout_s), __main__ (release-risk CLI)
services/api      /api/v1/release-risk routes
services/tool-gateway  agents.yaml (release_risk), compare_commits more_files
data/eval/release_risk_cases_v1.jsonl · scripts/smoke-phase15.sh · Makefile release-risk, risk-scan, risk-smoke
tests  agents/test_release_risk.py; codeparse/test_rules.py; api/test_gateway.py; integration/agents/test_release_e2e.py (5)
```

## 5–6. Commands (macOS, zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull
shasum -a 256 -c ../_to_delete/phase15-base.sha256 | grep -v ': OK$'   # no output = safe
git switch -c phase-15-release-risk
tar xzf ../_to_delete/phase15.tgz
uv sync --all-packages && make check
make db-upgrade                      # 0023: orchestrator.release_assessments
# then restart run-tools, run-agents, run-orch (new allow-list, routes and agent)
make test-integration
```
No re-seed and no re-index: the data did not change.

## 7. Config
`AEOI_AGENTS_RELEASE_*`: `max_commits` 10, `max_refs` 50, `task_deadline_s` 10.
`AEOI_ORCH_RELEASE_TIMEOUT_S` 12 (the api waits 15). Rubric constants: `BLAST_DEPENDENTS` 5.

## 8. Tests
- `make check`: 748 passed (sandbox). Integration: 228 passed, 1 skipped (sandbox).
- `make risk-scan`: 1,784 deploys → HIGH 2 (the planted ones), MEDIUM 102 (all "not checked"),
  LOW 1,680; labelled cases 6/6. Selectivity on synthetic code, NOT accuracy.
- Independent review: 2 high, 6 medium, 6 low; all fixed except M7 (documented, ADR-025).
- **Not measured anywhere yet:** latency on the Mac.

## 9. Run
```zsh
make dev ; make run-llm ; make run-rag ; make run-tools ; make run-audit ; make run-agents ; make run-orch
make risk-smoke                                   # Phase 15 end to end; prints the DEPLOY-4821 report
make release-risk PR=northwind/checkout#812       # the planted N+1, before it ships
make release-risk RANGE=checkout-api:<base>..<head>
make risk-scan                                    # offline: selectivity + labelled cases
make code-smoke                                   # regression (the code agent now shares scan_commit)
```

## 10. Verify (gate for Phase 16)
- [x] `make risk-smoke` 19/19 - the DEPLOY-4821 way back says a redeploy of 0742889 and that flipping order_batching would NOT help (read 2026-10-12)
- [x] `make risk-scan`: all as labelled; HIGH 2 / MEDIUM 102 / LOW 1,680 (same as the sandbox)
- [x] `make code-smoke` 21/21 after fix1 (20/21 before: the critic's long sentence, §11b)
- [x] `make test-integration` 228 passed, 1 skipped (Kafka), 6 min 43 s on the Mac

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| 503 `release risk not assessed` | agents down / slower than 12 s | `make run-agents`; nothing was stored |
| 403 from tool-gateway in the report error | `release_risk` not in agents.yaml (old config) | restart run-tools |
| PR #377 not HIGH, "references … not checked" | no code index | `make code-index` |
| band MEDIUM, `findings_band` LOW | part could not be checked (see `not_checked`) | expected; read why |
| status FAILED "swapped?" | range head older than base | swap them |

## 11b. fix1 (first Mac run)
`make code-smoke` failed once: the critic's `missing` sentence was longer than the schema's 200
characters, twice, so the whole critique was rejected (PARTIAL). Free text in every model schema
is now bounded at 1,200 (runaway only) and cut in code at a word (`aeoi_agents.common.clip`).
Test: `services/agents/tests/test_prose_limits.py` (the old schema rejects the Mac sentence).

## 12. Production considerations
Run on every PR from the code host and post the report as a PR comment (still advisory);
measure against real outcomes in Phase 25; repository ACL in `compare_commits`; never turn it
into a deploy gate without a new ADR and outcome data.

## 13–15. Interview
`docs/interview/phase-15-release-risk.md`.
