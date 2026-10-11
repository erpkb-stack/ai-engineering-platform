# ADR-024: Code intelligence by code rules over the deploy's diff; a finding is one more source for that deploy

Status: Proposed
Date: 2026-10-11
Refines: ADR-017 (tool gateway), ADR-020 (agents, evidence by kind), ADR-021 (candidates, rubric bands); architecture.md §10 #5 (Code Intelligence Agent, drawn as "LLM" - see Alternatives)

## Context
After Phase 13 an investigation knows WHICH deploy came before the symptom, but not WHAT it
changed. The data had commit messages and file names only: no code, no diffs. FR-5 asks for
"changed behaviours" from the deploy diff and for change impact ("who else uses this").
Constraints:
1. Code is readable per ROLE (`code:read`: not MANAGER) and, inside the code index, per GROUP
   (a repository's `allowed_groups`). What we store on an incident, every reader of the
   incident's evidence kind sees.
2. A finding must cite evidence (ADR-018). A model must not invent one.
3. The owner's Mac is Intel x86_64: tree-sitter wheels stop at 0.23.0 for that platform.

Owner decisions (2026-10-11): gate first (PR #13 merged); **full repository indexing** (warned:
large and slow on an Intel Mac); **code only** (no LLM reads a diff); influence = **evidence
for the deploy** (one more source in the unchanged rubric).

## Decision
```
collect_evidence ─► history ─► code (orchestrator node) ─► hypothesize ─► critique ─► validate ─► report
                               code_intelligence agent (as the user, delegated token), CODE ONLY:
                                 deploys started <= first symptom (max 3)
                                 compare_commits (tool gw ─► devdata: previous successful deploy's commit ─► this one)
                                 tree-sitter rules over each diff (aeoi_codeparse)
                                 find_references for a removed field (tool gw ─► rag, file ACL in SQL)
                                 COMMIT evidence (hunk) + CODE evidence (references shared with all engineers)
```
### A. Synthetic code (scripts/synth codegen.py)
30 repositories (`northwind/<domain>`), 90 deployable services in Java, Python and Go. Every
one of the 3,602 commits now has a real unified diff and the file after it
(`devdata.commit_diffs`, migration 0022); applying the diffs in time order gives HEAD
(unit-tested). Planted: commit #812 adds `orderRepository.findOpenOrders(item.getSku())` inside
the loop of `CartService.priceCart` (the demo deploy DEPLOY-4821); commit #377 renames
`ShipmentResponse.address` to `deliveryAddress` in shipping-api, still read by 4 other
services. codegen has its own seeded random stream; checkout-api is forced to Java AFTER the
seed's draw, so no other random value shifts - but checkout-api's own catalog row and the
knowledge documents naming its language change: re-seed and re-ingest.

### B. Parsing and rules (`libs/codeparse`, tree-sitter 0.23.0, grammars java 0.23.5 / python 0.23.6 / go 0.23.4)
The file BEFORE the change is rebuilt from the file after it and its diff (`pre_image`,
strict: a diff that does not fit the file raises, and the file is reported unchecked). Rules
compare the two parse trees:
| Rule | Fires when | Support? |
|---|---|---|
| `query_in_loop` / `remote_call_in_loop` | a call on a receiver named `*repo/repository/dao/db/store` / `*client` is in a loop BODY now, on an added line, and was not in a loop body of the same function before | yes (one per deploy, see D) |
| `limit_changed` | a numeric constant named like timeout/retries/pool/limit changed value | no: reported as a fact (810 of 3,602 synthetic commits: routine tuning) |
| `field_removed` | a field of a class/struct exists before and not after (removed or renamed) | no: impact on OTHER services |
Over the full synthetic history the rules find exactly the 2 planted changes plus the 810
limit changes; 0 diffs fail to fit their file (checked in the sandbox, seed 42).

### C. The code index (`rag.code_files/code_symbols/code_refs`, `make code-index`)
Every source file at HEAD of every repository that has an explicit ACL file
(`.aeoi-repo.json`, `allowed_groups`); a repository without one is not indexed. Unchanged files
(same sha256) are skipped; files gone from disk are deleted (symbols/refs cascade).
`POST /v1/code/references` (perm `code:read`): references by SIMPLE name; `Type.member` means
the member's accessor names (`address`, `getAddress`, `Address`) in services that also use the
type. The group filter is in the SQL, in the main query AND in the type sub-query, plus a
defence-in-depth assert. Each hit says `open_to_engineering`.

### D. Influence on reasoning - one more source, gated
A `query_in_loop`/`remote_call_in_loop` finding becomes ONE observation (role `code`) per
deploy, timed at that deploy's start, on its service. `candidates.code_support` adds it to the
deploy's support only when the deploy already has a symptom on its own service. So code can
turn "deploy + 1 symptom" (MEDIUM) into HIGH, but can never lift a deploy with no symptom of
its own, and three findings in one deploy count once. It never contradicts anything, is never
support for another cause, and the postmortem timeline skips it. Its result reaches
hypothesize/critique/validate/report only when its evidence was stored (`reasoning_results`).

### E. What may be stored and who sees it
COMMIT evidence: commit, author, PR, message and the hunk with the finding (`code:read`).
CODE evidence: one reference location (`code:read`), ONLY from files every engineer may read
(the incident is visible per role, not per repository group). The investigation plan and the
run's error are readable by every incident reader: they hold COUNTS only (`findings_by_rule`,
deploys/commits/files checked) - never a symbol, path or service list. The code trace needs
LOGS + METRICS + DEPLOYS + CODE read; so do the ranker, critic and writer traces now.

### F. Outcome
Anything not fully checked is `degraded` → the run is PARTIAL, named generically ("code only
partly checked (see the trace)"): a deadline, a file too long for the gateway (both texts
emptied and flagged, never parsed half), a diff that does not fit its file, more commits than
`max_commits`, more references than `max_refs`. A redeploy of the same commit or a rollback
shipped nothing new: `compare_commits` says `same`/`rollback` and returns nothing.

## First Mac run (2026-10-11, Intel x86_64)
`make code-smoke` 21/21: one `query_in_loop` on `CartService.priceCart` (PR #812), the COMMIT
hunk shows the per-cart query replaced by a per-item query inside the loop; deploy hypothesis
HIGH with the commit as support; 4 impacted services for `ShipmentResponse.address`; MANAGER
sees counts only. Index: 30 repositories, 302 files, 0 parse errors, 0 skipped. Re-index with
nothing changed: 13.8 s wall (302 files skipped by sha) - the FIRST full index was not timed.
Regressions: history 23/23, postmortem 22/22, hypotheses 24/24; integration 223 passed.

Seen in the run (not fixed here): the critic and the writer proposed "set order_batching back
to false" as a mitigation, but the per-item query in the diff is NOT behind that flag - the
code evidence contradicts the action. Nothing checks a proposed action against the code yet
(input for Phase 15/16: rollback, not the flag).

## Alternatives
| Option | Why not |
|---|---|
| LLM reads the diff (architecture.md drew "LLM") | owner chose code only; a model can invent a mechanism, and every rule here is checkable |
| Regex rules on the patch (first build) | review found false positives (loop headers, re-indented lines, function parameters) and misses (Go tags, Java annotations); pre-image + parse fixes them |
| `limit_changed` as support (first build) | fired on most deploys; lifted unrelated deploys a band |
| Code as support without a symptom gate (first build) | "a mechanism exists" is not "it fired" |
| Type resolution (javac / go/types / pyright) | needs a build per language and repo; out of scope for [P] |
| Index only the deployed services | the owner chose full indexing; impact needs the consumers |

## Review (independent, read-only subagent) - 3 high, 7 medium, 8 low
Fixed with tests: code symbols, impacted services and failing symbols reached MANAGER through
the investigation plan and error (H1); code findings raised bands without any relation to the
symptom, and `limit_changed` was support (H2); a redeploy/rollback analysed the old head
commit as new code (H3); references from restricted repositories could be stored on an
incident (M1, now `open_to_engineering` only); the critic's `refinement_refs` were not
redacted (M2, an older gap); rule false positives/negatives (M3); the gateway silently cut
long diffs (M4); partial checks finished COMPLETE (M5); facts could cite evidence dropped by
the 100-item cap (M6); a misleading "nothing else shifts" comment (M7); unused tools in the
agent's allow-list; one code observation per deploy; removal excerpts; trace permissions.
Also added: tests for the previous-deploy subquery, the range edges, crash + resume of the
code node. Mutation-checked: ACL in the main query and in the type sub-query, type filter,
loop body, "already in a loop", parsed field diff, one-source cap (2), code support in
candidates, same-timestamp head, `refinement_refs` redaction.

## Honest limits
- Simple names, no type resolution: "who reads `address`" is a heuristic narrowed by "uses the
  type". Python consumers that never name the type are missed.
- Calls inside lambdas / streams / comprehensions are not seen (a function boundary).
- `compare_commits` reads `devdata` with no repository ACL: any `code:read` user can read every
  repository's diffs. Harmless today (every synthetic repo is `eng-all`); a real code host must
  enforce repository permissions there.
- Commits are ordered by time, not by the merge graph; commits that tie with the base on time
  are left out.
- Rules find a MECHANISM; they do not prove it fired. That is why it is one source among
  several and needs a symptom on the same service.

## Tradeoffs
- tree-sitter is pinned to the last Intel-Mac release (0.23.0); newer grammars use an ABI it
  cannot load.
- Full indexing: every file of 30 repositories on each `make code-index` (unchanged files are
  skipped by sha). Time on the Mac: not measured yet.
- One more agent call per investigation (no model cost; two tool calls).

## Prototype vs Production vs Enterprise-scale
[P] synthetic repos on disk, tree-sitter at HEAD, simple names. [Prod] a code host API
(GitHub/GitLab compare + permissions), index on push (webhook), SCIP/LSIF or a compiler for
type-resolved references, repository ACL enforced in `compare_commits`. [Ent] monorepo scale
(incremental indexing per changed file), per-tenant code search, retention of evidence hunks.
