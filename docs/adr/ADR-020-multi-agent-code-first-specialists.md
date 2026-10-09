# ADR-020: Multi-agent investigation with code-only specialists, PARTIAL outcomes, rag on-behalf-of

Status: Proposed
Date: 2026-10-08
Refines: ADR-018 (agent pattern), ADR-019 (graph, delegation; closes its "rag refuses delegated calls")

## Context
Phase 10 turns the one-agent graph (ADR-019) into a real fan-out. architecture.md §10 lists six
specialists; Code (Phase 14) and Historical (Phase 13) have their own phases, and Triage adds
nothing while every incident already names its services. That leaves **Metrics, Deployment,
Knowledge** next to Log Analysis.

Constraints that shaped the decision:
1. The owner's Mac is an Intel CPU: one llama3.2:3b call takes 60–110 s, and Ollama answers one
   call at a time. "Parallel agents cut wall-clock time" (§9) is **false for LLM work there**.
2. What these three agents produce - anomaly onsets, deploy timing, retrieval hits - is
   arithmetic and lookup. ADR-018: the LLM must never write facts; here it has nothing else to add.
3. Delegated tokens are accepted only in the on-behalf-of slot (ADR-019), and rag verified the
   user as a PRIMARY bearer, so knowledge tools refused delegated calls (501).
4. rag ACLs are per GROUP; incident evidence is visible to anyone who may read the incident.

## Decision
### A. Three code-only agents (no LLM client is even passed to them)
- **metrics** (`query_metrics`): per (service, metric), ONE query covering a 120-min baseline
  before the window plus the window. Robust z-score per bucket: `(x - median) / max(1.4826·MAD,
  2 %·|median|, absolute floor per metric)`. Runs of |z| ≥ 4: *shift* (≥ 3 buckets, still going
  at the last bucket), *transient* (ended, or short and still going - worded as such). One
  Fact per series: the anomaly, OR "stayed within its baseline" - a fact on purpose, because
  "traffic did not move" is what rules out a surge in Phase 11. Single-bucket spikes are never
  called "within baseline". A metric the service does not report is a note (nothing to cite).
- **deployment** (`get_deployment`, `get_config_diff`): deploys from 6 h before the window to its
  end; config diffs (secret values arrive redacted; "[20, 20]" is touched, not changed). One Fact
  per deploy with minutes from deploy **start** to detection, and "still rolling out at
  detection" when it spans it. Timing only - "this deploy caused it" is a Phase 11 hypothesis.
- **knowledge** (`search_runbooks`, `search_docs`): **pointers only** - document id, chunk id,
  rank, evidence id. No text, no title (see D). No facts: a retrieval rank is not a fact about
  the incident. Phase 11 re-fetches the text as the user when it reasons about runbook steps.

### B. The graph: plan in code, PARTIAL outcome
- `plan` runs every configured agent (`AEOI_ORCH_AGENTS`; a start may ask for a subset). An LLM
  planner choosing among four cheap read-only agents would spend a model call to save tool calls.
- Each `Send` branch records its own result; a bad task fails its branch, not the run.
- Outcome: all SUCCEEDED → **COMPLETE**; some failed → **PARTIAL** (error names the missing
  sources); none → **FAILED**; evidence not stored → FAILED (repairable by `repost-evidence`,
  which then yields COMPLETE or PARTIAL). Investigation deadline: finished tasks' evidence is
  salvaged → PARTIAL, live tasks TIMED_OUT.
- Migration 0019 adds PARTIAL to the status CHECK (superset swap; downgrade turns PARTIAL into
  FAILED and says so in `error`).
- Model comparison (`agent-compare`) runs only `log_analysis`: the others have no model.

### C. rag on-behalf-of (closes ADR-019's open item)
- The tool-gateway always calls rag with **its own service token (scope `rag:obo`)** plus the
  caller's on-behalf-of token unchanged (user or delegated) in `X-On-Behalf-Of`. One path for
  both - a delegated token is still never a bearer anywhere.
- rag: `aeoi_web.require_acting_user(perm, "rag:obo")` → the principal is the on-behalf-of USER;
  their groups go into the SQL filter. A service bearer without the scope or the header: 403.
- Delegated tokens are accepted on `/v1/search` and `/v1/incidents/search` only - not on
  `GET /v1/documents/{id}` (the investigation flow never opens whole documents).

### D. Evidence visibility by kind (review finding, pre-existing since Phase 8)
`GET /incidents/{id}/evidence` returned raw log lines to anyone with `incidents:read` (MANAGER,
ADMIN). Now each kind needs the permission of the tool that produced it
(`aeoi_security.rbac.EVIDENCE_KIND_PERMS`, filtered in SQL, unknown kind = hidden). The trace
uses the same rule per agent. Knowledge evidence is a pointer for the group reason in Context 4.

## Alternatives
| Option | Why not |
|---|---|
| LLM labels/summaries in the new agents (ADR-018 pattern) | +3 model calls (≈3–5 min on the Mac, serialized) to reword numbers code already got right |
| LLM planner node | a model call to choose among 4 cheap, read-only agents; revisit when agents are costly |
| Mean/stddev anomaly test | one spike in the baseline inflates σ and hides the real change (test pins it) |
| Store knowledge text + title on the incident | leaks group-restricted docs to every incident reader |
| Keep user-token-as-bearer to rag, add a delegated path beside it | two auth paths to test and keep consistent; and it presents user tokens as bearers to a service that isn't the edge |
| COMPLETE with an error, or FAILED, when one agent fails | COMPLETE hides a missing source ("found nothing" vs "did not run"); FAILED discards good evidence |
| Triage agent now | the incident record already has services and a window; an LLM call for no new information |

## Review (independent, read-only subagent) - 3 high, 4 medium, 6 low; 9 fixed (7 with a regression test)
Fixed: "stayed within baseline" stated for a series with z = 300 single spikes; a short run at
the window end called "back within baseline"; a mostly-zero count made 0.2 errors/min an
anomaly (absolute floor per metric); a rollout spanning detection printed as "AFTER" (now from
the start); one hung agent at the deadline discarded the other agents' evidence (salvage →
PARTIAL); an invalid task failed the whole run; windows > ~18 h broke the gateway's 24 h cap
(look-backs clamped, start validates ≤ 24 h); delegated tokens could open whole documents in
rag; facts said "to the window end" when data stopped earlier (now the last bucket's time); an
anomaly could cite evidence dropped by the fact budget; a bucket straddling the window start
was counted as baseline. Found by the live run, not the review: the PII scrubber turned a
digit-only UUID into `[CARD]…` (canonical UUIDs now pass the scrubber).
Not fixed (documented below): DOC pointer existence, rate-limit on retry, idempotent replay
ignores a different body. No change: `investigate` exits 1 on PARTIAL (intended: not all
sources answered). No dedicated test yet: the 24 h clamp, "to the last bucket" wording, the
bad-task branch.

## Tradeoffs
- **No speed-up on the Mac.** The three new agents finish in < 1 s each; the run still waits
  for the log agent's model call. The parallelism win is real only for I/O-bound agents.
- **Pointers reveal existence.** A `docs:read` user outside a doc's groups sees that *some*
  document (by id) matched. No title or text. [Prod] filter references through rag per reader.
  A RUNBOOK pointer needs `docs:read` to open (rag's document route), not just `runbooks:read`.
- **Rate limits.** Metrics makes up to 18 `query_metrics` calls (burst 20 per user+tool). Two
  investigations by one user in the same minute, or an orchestrator retry of the metrics task,
  get `rate_limited` calls → the result is degraded with those series named, not silent.
- **Idempotent replay** returns the first run for the same key even if `agents` differs (as before).
- **Fixed thresholds** (z ≥ 4, 3 buckets, floors) are judgement, not calibrated. [Prod] tune on
  labelled incidents (Phase 25) and seasonality-aware baselines (same hour last week).
- **The gateway holds a `rag:obo` token.** Its compromise = search as any user whose token it
  also holds (it already sees them). [Prod] mTLS/workload identity + short-lived tokens.

## Consequences
- Phase 11 (critic, hypotheses) gets typed evidence from four sources, incl. negative facts.
- Phase 13/14 add Historical/Code agents as more `Send` targets; the pattern here is the template.
- Phase 12 builds the timeline from the facts' timestamps (code, not LLM).

## Prototype vs Production vs Enterprise-scale
[P] fixed thresholds, one baseline window, all agents every run, rag token in a file.
[Prod] seasonality baselines, per-service metric catalog (no blind 9-metric sweep), cost-aware plan.
[Ent] per-tenant agent sets and budgets, reference filtering per reader, central rate quotas.
