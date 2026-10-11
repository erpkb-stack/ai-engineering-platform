# Phase 14 — Code intelligence

## 0. Gate and scope challenge
- **Gate: passed.** PR #13 merged; Phase 13 verified on the Mac (`make history-smoke`,
  `make pm-smoke`, `make hypo-smoke` all ✔).
- **Challenge 1 — "full repo indexing"** (owner choice). Warned: large and slow on an Intel
  Mac. Kept, with sha skip on re-index; the time on the Mac is not measured yet.
- **Challenge 2 — "code only".** Accepted: every rule is checkable and a model would add an
  uncheckable mechanism. architecture.md §10 #5 drew this agent as "LLM"; ADR-024 records why
  not.
- **Challenge 3 — "evidence for the deploy".** Kept, but gated after review: code proves a
  mechanism EXISTS, not that it fired. So it counts once per deploy, only for a loop
  query/remote call, and only when that deploy already has a symptom on its own service.

## 1. Objective
For each deploy before the first symptom: what did it ship, does the diff add a database or
remote call inside a loop, did it change a limit or remove a field - and if a field was
removed, which other services still read it. Every answer cites a commit or a code location.

## 2. Business reason
"The deploy did it" (Phase 11) is a timing argument. Engineers then open the PR to find the
mechanism. This phase brings the mechanism (or its absence) into the investigation, with the
hunk, and says who else a breaking change affects.

## 3. Architecture
```
collect_evidence ─► history ─► code ─► hypothesize ─► critique ─► validate ─► report ─► finalize
                               code_intelligence (delegated token = the user, NO model)
                                 compare_commits ─► tool gw ─► devdata (previous OK deploy ─► this one)
                                 aeoi_codeparse: pre-image + tree-sitter rules
                                 find_references ─► tool gw ─► rag /v1/code/references (file ACL in SQL)
make codepack ─► data/sample-repositories ─► make code-index ─► rag.code_files/symbols/refs
```
| Guarantee | Enforced by | Test (mutation-checked) |
|---|---|---|
| File ACL in the SQL (main query + type sub-query) | `codeindex._REFS_SQL` + assert | `test_a_restricted_file_never_leaks_through_the_api[*]` (ACL removed → fails) |
| Repo without an ACL file is not indexed | `index_repos` | `test_a_repository_without_an_acl_file_is_not_indexed` |
| `Type.member` matches only services that use the type | type sub-query | `test_a_field_with_the_same_name_on_another_type_is_not_impact` (filter removed → fails) |
| Re-index skips unchanged, deletes gone files (cascade) | sha + delete | `test_reindex_skips_unchanged_files_and_drops_deleted_ones` |
| MANAGER cannot read code (rag, tools, evidence, trace, plan, error) | `code:read`, `EVIDENCE_KIND_PERMS`, counts-only plan | rag 403, tools 403, e2e MANAGER checks |
| Only references every engineer may read are stored | `open_to_engineering` filter in the agent | unit (restricted ref never stored) |
| Range = previous successful deploy (same env) → this deploy; redeploy/rollback = nothing new | `get_deployments.prev`, `compare_commits.relation` | `test_previous_commit_is_…`, `test_compare_commits_…` (same-timestamp regression) |
| Loop call = in the BODY, new vs the pre-image | `_in_loop`, `calls_in_loops` | `libs/codeparse/tests` (header, re-indent: 2 mutations) |
| Removed field = parsed, not pattern-matched | `fields_removed` (pre vs post symbols) | parameter / tag / annotation cases (1 mutation) |
| One code source per deploy, only with an own-service symptom | `observations.build`, `candidates.code_support` | unit (2 mutations) + e2e (support removed → fails) |
| Not fully checked = PARTIAL | `degraded` → `code_outcome` | unit (deadline, too long, mismatch, more) + e2e (agent down) |
| Every cited evidence id is returned | cap then filter | `test_every_cited_evidence_id_is_returned_even_past_the_cap` |
| Code node not re-run on resume; evidence stored once | reuse guard, idempotent evidence | e2e `test_crash_after_code_…` |

## 4. Files
```
docs/adr/ADR-024-code-intelligence.md (+ README row) · docs/phases/phase-14.md · docs/interview/phase-14-code.md
libs/codeparse (new: aeoi_codeparse, tree-sitter 0.23.x pins, tests/test_rules.py)
libs/db        models rag (CodeFile/CodeSymbol/CodeRef), devdata (CommitDiff), incident (CODE kind);
               alembic 0022_code_index_and_commit_diffs
libs/models    api/code.py (new), api/search.py (CodeReferences*), api/agents.py, api/hypotheses.py, findings.py
libs/security  rbac (CODE → code:read)
scripts/synth  codegen.py (new), generate.py (code(), checkout-api = java), load.py, __main__ (codepack)
services/rag   codeindex.py (new), api.py (/v1/code/references), __main__ (index-code)
services/tool-gateway  compare_commits, find_references, get_deployment.previous_commit_sha, agents.yaml
services/agents code/agent.py (new), reasoning/observations.py + candidates.py, postmortem (skip code), config/main/api
services/orchestrator  graph (code node, code_outcome, reasoning_results), api perms, config `code`
services/incident-service  MODEL_TEXT + refinement_refs
scripts  smoke-phase14.sh (new); smoke-phase9/10/11 exclude the new task
tests    integration rag/test_codeindex.py (9), agents/test_code_e2e.py (3), tools (3 new);
         agents/test_code_agent.py; orchestrator/test_graph_units.py; synth/test_codegen.py
Makefile codepack, code-index, code-smoke
```

## 5–6. Commands (macOS, zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull                  # brings the PR #13 merge
shasum -a 256 -c ../_to_delete/phase14-base.sha256 | grep -v ': OK$'   # no output = safe
git switch -c phase-14-code
tar xzf ../_to_delete/phase14.tgz
uv sync --all-packages                       # tree-sitter 0.23.0 + 3 grammars (x86_64 wheels)
make check
make db-upgrade                              # 0022: code tables, commit_diffs, evidence kind CODE
make db-seed                                 # commits now carry real diffs; checkout-api is java
make run-llm                                 # then, in another terminal:
make rag-ingest && make embed-history        # db-seed truncated documents + past incidents
make codepack && make code-index             # the repositories, then the index
make test-integration
```

## 7. Config
`AEOI_AGENTS_CODE_*`: `max_commits` 10 (per deploy), `max_refs` 50 (per removed field),
`task_deadline_s` 60. `AEOI_ORCH_CODE` (true; needs reasoning on). Agent constants:
`MAX_DEPLOYS` 3, `MAX_REFS_PER_FINDING` 10 stored, `MAX_EVIDENCE` 100. Gateway: a file or diff
over 4000 chars is not passed on (flagged `truncated`; the largest synthetic file is 3349).

## 8. Tests
- `make check`: 700 passed (sandbox). Integration: 223 passed, 1 skipped (sandbox, fake models).
- Over the full seed-42 history (3,602 diffs): 0 diffs fail to fit their file, 0 over the
  gateway limit; rules find the 2 planted changes + 810 limit changes (not support).
- Independent review: 3 high, 7 medium, 8 low; all fixed or documented (ADR-024 "Review").
- **Not measured anywhere yet:** index time and size on the Mac, end-to-end latency.

## 9. Run
```zsh
make dev ; make run-llm ; make run-rag ; make run-tools ; make run-audit ; make run-agents ; make run-orch
make tools-tokens && make agents-tokens     # only if a token file is older than its scopes
make code-smoke                              # Phase 14 end to end; prints the COMMIT hunk
make history-smoke && make pm-smoke && make hypo-smoke   # regressions (each now runs the code agent)
```

## 10. Verify (gate for Phase 15)
- [ ] `time make code-index` prints `"parse_errors": 0` and no `!!` line; write down the time
- [ ] `make code-smoke` all ✔ - read the printed hunk: is the call really inside the loop?
- [ ] `make history-smoke`, `make pm-smoke`, `make hypo-smoke` still ✔ (deploy band still HIGH)
- [ ] `make test-integration` green

Measured here: sandbox only, fake embeddings and fake models — no latency or cost numbers claimed.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| smoke: `find references: … no hits` | no index yet | `make codepack && make code-index` |
| `code_intelligence=SUCCEEDED` but `findings_by_rule` empty | old seed (commits without diffs) | `make db-seed`, then re-ingest (§5) |
| PARTIAL `code_intelligence: code only partly checked` | deadline, file too long, diff not fitting, > max_commits/refs | read the code trace (SRE) |
| PARTIAL `code_intelligence: FAILED (see the trace)` | tool denied / tool-gateway down | `make run-tools`; agents.yaml lists compare_commits, find_references |
| tasks list has no `code_intelligence` | `AEOI_ORCH_CODE=false` or reasoning off | unset it; restart run-orch |
| `uv sync` builds tree-sitter from source | a newer tree-sitter resolved | keep the 0.23.x pins (Intel Mac) |
| `make db-upgrade` downgrade refuses | CODE evidence rows exist | expected: delete them first, by hand |

## 12. Production considerations
A code host API with its permissions in `compare_commits`; index on push; type-resolved
references (SCIP/LSIF or a compiler); rules per framework (ORMs, HTTP clients) with a
labelled set of real N+1 PRs to measure precision.

## 13–15. Interview
`docs/interview/phase-14-code.md`.
