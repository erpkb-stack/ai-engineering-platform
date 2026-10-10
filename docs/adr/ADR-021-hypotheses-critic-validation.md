# ADR-021: Hypotheses by code, ranking by Haiku, critique by Sonnet, validation by code

Status: Proposed
Date: 2026-10-09
Refines: ADR-018 (facts by code), ADR-020 (code-only evidence agents); architecture.md §9–10

## Context
Phase 10 produces typed evidence from four agents (log clusters, metric anomalies with onsets,
deploys with config diffs, knowledge pointers). Phase 11 must turn it into ranked root-cause
hypotheses, have an independent critic attack them, and validate the result before anything is
stored on the incident. Constraints:
1. A model must never invent a cause, a time, a number or a confidence (ADR-018; rules/langgraph-agents).
2. The critic must be independent of the author (architecture §9 reason 4) and get the evidence,
   not the author's prose (rules/langgraph-agents).
3. Intel-CPU llama is too slow and too weak to critique (architecture §1). Owner decision
   (2026-10-09): **Haiku ranks (`fast`), Sonnet critiques (`reasoning`)**.
4. Owner decision: **core only** — no LLM judge (nothing measures the judge until the eval
   phase) and no critic→plan / judge→hypothesize loops.

## Decision
```
collect_evidence ─► hypothesize ─► critique ─► validate ─► finalize
                    (agents)       (agents)    (orchestrator, code)
```
### A. Observations (code) — `services/agents/.../reasoning/observations.py`
The evidence agents' typed results become a time-ordered list of observations with a role
(`effect`, `resource`, `traffic`, `change`, `steady`) and refs `o1..oN`. Both reasoning agents
rebuild them independently; the critic refuses unless the SHA-256 of its observations equals
the ranker's (`observations_sha`). Log-derived text (error codes) is pattern-checked and every
statement is flattened to one printable line, so data cannot forge an observation line.

### B. Candidates and confidence (code) — `candidates.py`
Code proposes every cause and computes its band with an evidence rubric (HIGH needs ≥ 3
supporting observations from ≥ 2 agents and nothing against; deploys > 60 min before the
first symptom are capped at MEDIUM; critic alternatives are capped at MEDIUM). Timing rules use
the first **sustained** metric shift; a log cluster cannot time a cause out (background noise).
A deploy's support is its own service only; other deploys before the symptom count against it.
Causes that steady metrics or timing exclude are stored as REJECTED ("ruled out").

### C. Ranking (Haiku, `fast`) — `hypothesis.py`
The model orders the candidate ids and writes ≤ 2 sentences each, citing observation refs.
Unknown ids, invalid refs and uncited prose are dropped; a skipped candidate is ranked by
the rubric and the run says so. The model cannot add a cause or change a band.

### D. Critique (Sonnet, `reasoning`, **fallback off**) — `critic.py`
Gets the observations and the candidates as CODE wrote them — never the ranker's explanation
or rank (removed twice: in the graph and in the agent). Returns per-candidate verdict,
contradicting refs and missing evidence, plus ≤ 2 alternatives with refs, or a reason why
there is none. Code accepts a contradiction only if the ref exists, is not the candidate's
own support, AND code says that kind of observation can contradict that kind of cause
(`candidates.can_contradict`: for a deploy, a symptom on its service that started before it,
or another deploy before its first symptom; for saturation/traffic, a symptom before its onset
or the same metric steady). Other cited refs are stored as `critic_disputed`: shown to the
engineer, never in the band. An alternative the critic marks `refines: hN` is stored as
hN's `refinement` (a more specific mechanism, not a rival) only if hN is a candidate deploy
and the alternative cites that deploy's own observation. The route is not overridable per task; with
`allow_fallback=false` a Sonnet failure is an error, never a quiet llama critique.

### E. Validation (code) — `orchestrator/validation.py`
Hard checks (a failure drops the hypothesis and makes the run PARTIAL): cited evidence was
stored by this investigation; ≥ 1 supporting evidence; timing against the first sustained
symptom. Soft checks (PARTIAL, nothing dropped): the critic answered (alternative or reason);
the critic's model differs from the ranker's (`critic_independent`). Status: VALIDATED only if
the critic called it `supported`; CHALLENGED if the critic cited a contradiction code accepts;
otherwise (weakened, refuted without checkable evidence, critic alternatives) PROPOSED.
The run summary reports `kept` (passed the hard checks) and `validated` (status VALIDATED).

### F. Storage and visibility — incident-service, migration 0020
`incident.hypotheses` gains `investigation_id`, `hypothesis_key` (unique per investigation →
idempotent POST) and `detail` JSONB. Edges go to `hypothesis_evidence` (SUPPORTS/CONTRADICTS).
`GET /incidents/{id}/hypotheses`: code-written statements for every incident reader; edges
filtered by evidence kind (ADR-020); **model-written text** (explanation, critic's "missing",
a critic alternative's statement) redacted for a reader who cannot read all of its evidence.

### G. Outcome
COMPLETE only if every step ran, nothing was dropped, the ranking was by the model, and the
critic answered and was independent. No surviving hypothesis → INCONCLUSIVE. Otherwise PARTIAL
with the reasons. A run whose evidence was reposted later is PARTIAL ("hypotheses not generated").

### H. Gateway fixes found on the way
`claude-sonnet-5-5` rejects `temperature` (HTTP 400; Mac, 2026-10-09): `supports_temperature:
false` per model in routing.yaml. Such a model's answers are not cached (they are sampled).
`make llm-smoke` now calls every hosted-primary route with fallback off.
Found on the Mac with the first real run: the same model also rejects a FORCED tool choice
(HTTP 400 'tool_choice: type "tool" and "any" are not supported for this model'), which is how
the gateway produced structured output. `supports_forced_tool: false` per model: the gateway
offers the single tool with `tool_choice: auto` plus an instruction; the arguments are still
validated against the schema (one repair, then an error). `llm-smoke` now makes a plain AND a
structured call on every hosted route — it only tested structured output on `fast` before.

### I. First real run (Mac, 2026-10-09, INC-10015) — eval case #1
Sonnet called the deploy `supported` but listed "cpu_util steady" and "traffic steady" as
contradicting it; code counted any existing ref, so the deploy fell HIGH → LOW and was
CHALLENGED. Its alternative ("the order_batching flip in DEPLOY-4821 is the trigger") was
stored as a rival MEDIUM hypothesis — a restatement ranked as stronger than the original.
Every check passed; only reading the output found it. Fix: `can_contradict` (code decides
what can count against a cause), declared + verified `refines`, critic prompt v2 (defines
"contradicting"), `supported` required for VALIDATED. Replayed as a unit test.

## Alternatives
| Option | Why not |
|---|---|
| LLM writes hypotheses from the facts | invents causes, times and confidences; untestable; injection reaches conclusions |
| LLM-assigned confidence (%) | uncalibrated; rules require an evidence-rubric band |
| Same model ranks and critiques | not independent; cheaper. Chosen against by the owner |
| Critic sees the ranker's explanation | it would grade a story it was told |
| LLM judge for "is the claim supported" | unmeasured until labelled data exists (eval phase) |
| Critic→plan loop for more evidence | the evidence agents are deterministic and cheap; a loop adds latency without new sources |

## Review (independent, read-only subagent) — 4 high, 6 medium, 2 low
Fixed with tests: background log noise could reject the real deploy (sustained anchor);
every early deploy was HIGH (own-service support, competing deploys); a newline in an error
code could forge an observation; > 12 ruled-out causes crashed the agent; model-written text
was visible to roles that cannot read the evidence; the timing check could never fail and
alternatives / refuted candidates were stored VALIDATED; one error code split into templates
counted twice; a reposted run became COMPLETE without reasoning; dropped hypotheses and a
rubric fallback still gave COMPLETE; truncation dropped negative evidence first; the critic
checked refs, not the observation set; "caused" before validation; sampled Sonnet answers
were cached. **Not fixed:** `citations_stored` uses the evidence batches this investigation
posted, not a read-back from incident-service — incident-service still refuses the whole batch
(422) if any edge points at missing evidence, so nothing wrong is stored, but one bad key
loses all hypotheses of the run (documented, PARTIAL). Retries on 502/503 can repeat a paid
Sonnet call (bounded by the per-investigation budget and deadline).

## Tradeoffs
- **Cost:** one Haiku + one Sonnet call per investigation (cents). Measured on the Mac only.
- **Rules are judgement:** z ≥ 4, 60-min lead cap, "3 from 2 sources" for HIGH are not
  calibrated. [Prod] calibrate on labelled incidents (Phase 25).
- **Code proposes, so code limits.** A cause no rule covers (e.g. a dependency outage with no
  metric) can only come from the critic's alternatives, capped at MEDIUM.
- **MANAGER sees code conclusions** (deploy keys, metric names) but no evidence or model text.

## Prototype vs Production vs Enterprise-scale
[P] fixed rubric, one critic pass, synchronous agents. [Prod] calibrated bands, judge with an
eval set, loops with budgets, async workers (Phase 18). [Ent] per-tenant model policy,
human review queue (Phase 16) before any hypothesis drives an action.
