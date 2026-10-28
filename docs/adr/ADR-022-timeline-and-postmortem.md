# ADR-022: Timeline by code, postmortem narrative by Sonnet, document assembled at read time

Status: Proposed (refined by ADR-045, Phase 29c: removed sentences are kept with the reason and shown apart)
Date: 2026-10-10
Refines: ADR-018 (facts by code), ADR-020 (evidence visibility by kind), ADR-021 (hypotheses); architecture.md §9 ('report', TimelineBuilder), FR-5, FR-13

## Context
After Phase 11 an investigation has typed observations (o1..oN) and validated hypotheses.
FR-5 asks for a timeline (deploys, config, logs, metrics, alerts, human actions); FR-13 for a
postmortem with evidence references. Constraints:
1. A model must not invent a time, a number, a cause or a confidence (ADR-018, ADR-021).
2. Visibility: a reader who may not read LOG/METRIC evidence must not read it through a
   timeline row or a postmortem sentence (ADR-020).
3. A root cause is only *confirmed* by a human; that flow is Phase 16.
4. incident-service never calls a model and must work when every AI service is down.
Owner decisions (2026-10-10): build before the gate (Phase 9/10 crash + resume not yet run on
the Mac); **full postmortem now** (root cause and action items included); **Sonnet writes it**.

## Decision
```
validate ─► report (orchestrator) ─────────────────────────────────► finalize
            postmortem agent:  timeline (code) + numbers (code)
                               Sonnet (`reasoning`, fallback OFF): sections + action items
                               check (code): refs, numbers, markup
            POST /timeline   (points, idempotent per source|service|subject|instant)
            POST /postmortems (generated parts + this run's points, idempotent per run)
GET /postmortem (incident-service): assembled NOW from the record + stored parts, per reader
```
### A. Timeline (code) — `postmortem/agent.py: timeline()`
Every non-steady observation becomes a point (`OBSERVED_CHANGE/EFFECT/RESOURCE/TRAFFIC`) with
its time, statement, evidence keys and observing agent; the detection time is a SYSTEM point.
Steady observations are **negative evidence** ("did not change") with their evidence keys.
Points are stored in `incident.incident_events` (the existing timeline table), so GET
/timeline merges human actions and agent observations. `source` is **derived by
incident-service from the stored evidence kind**, never trusted from the caller; a point whose
evidence needs more than one permission is refused. Dedupe key = source|service|subject|UTC
instant (migration 0021, unique per incident).

### B. Narrative (Sonnet) and its checks (code)
The writer sees the observations with times, the validated hypotheses (critic refinement
marked as model text), the ruled-out causes and a NUMBERS block computed by code. Code keeps a
sentence only if it (1) cites ≥ 1 existing observation that has evidence, (2) contains no
link/markup, (3) contains no date or number absent from the code-written facts it could rest
on (its cited observations, the computed numbers, the hypothesis statements). Digit-led words
that occur in the input ("5xx") are names; "900ms", "3x" and "12 minutes" are numbers.
Dropped sentences are counted (`sentences_dropped`, `numbers_rejected`) and shown.

### C. Root cause, numbers, state (code)
The root-cause section is the stored top hypothesis of **that** investigation, verbatim, with
its band and status, labelled "most likely" and "**not yet confirmed by a human**"
(`confirmed_by_human` is true only for status CONFIRMED, which Phase 16 will set). Header
numbers (first sustained symptom, trigger start, minutes to symptom / detection / resolution)
are computed by incident-service from **that run's own points** and the incident record; the
trigger is the observation code chose (`trigger_ref`: the top CODE hypothesis' deploy). When
there is no root cause, `root_cause_state` says why: none survived / reasoning failed /
reasoning off / not stored — "none survived" never stands in for "not analysed".

### D. Assembly and visibility — incident-service, migration 0021
`incident.postmortems` holds the generated parts per (incident, investigation). GET
/postmortem assembles the document at read time: status, resolution and human events are
always current. Model sentences, action items and negative evidence are **redacted** (text,
refs and evidence ids) unless the reader may read every evidence kind they cite. Timeline rows
are filtered in SQL by source kind; GET /timeline returns `X-Hidden-Rows`. Markdown is a code
template; model text is escaped.

### E. Outcome
A writer that failed, wrote nothing usable, or was not stored makes the run PARTIAL, named
(`report_outcome`); worse statuses win. The writer's trace is readable with the evidence
permissions (like the critic's).

## Alternatives
| Option | Why not |
|---|---|
| Model writes the whole postmortem | invents times, numbers and causes; nothing to check |
| Store the rendered document | goes stale the moment the incident is resolved; one redaction for all readers |
| Timeline from raw evidence rows | one row per log line; metric rows have no onset; statements are code facts already (observations) |
| A separate timeline table | `incident_events` is the timeline (FR-5, check constraint has the sources) |
| Haiku writes (cheaper) | owner chose Sonnet; prose quality is the point of this step |
| Postmortem only after resolution | demo and MTTR value need the draft during the incident; the read-time assembly keeps it current |

## Review (independent, read-only subagent) — 4 high, 5 medium, 3 low
Fixed with tests: negative evidence leaked METRIC data to MANAGER; the dedupe key ignored the
service (two services' p95 at one minute = one row); header numbers mixed investigations;
"900ms"/"3x" passed the number check and the whole prompt was the corpus; "no hypothesis
survived" printed when reasoning had failed; timeline visibility trusted the caller's source;
trigger matched by substring; redacted sentences kept evidence ids; negative durations printed
as "-4 min after"; markup/links from model text; GET /timeline gave no hint rows were hidden;
naive datetimes; an observation with no evidence crashed the check. Found by live runs, not
review: times labelled Z were the DB session's local time (-05:00) in the writer's input and
the markdown; the dedupe key changed with the offset.
**Not fixed (documented):** a sentence is redacted by the evidence it CITES, but the model saw
every observation, so it could quote an uncited one. No shipped role has partial evidence
permissions today (ENGINEER-family: all; MANAGER: none), so this is latent; GET /hypotheses
has the same property. [Prod] tag sentences with every kind in the prompt, or filter the
writer's input per reader.

## Tradeoffs
- **Cost/latency:** one more Sonnet call per investigation (~30–60 s, cents). Measured on the
  Mac only. Every smoke now pays it.
- **The number check is a heuristic.** It blocks invented quantities and dates; it cannot
  catch a wrong claim made of known numbers ("7 min" used for the wrong gap).
- **Two clocks in one table:** incident time (observations) and record time (human/agent
  actions) are merged by timestamp; with synthetic data they are days apart.

## Prototype vs Production vs Enterprise-scale
[P] one writer pass, read-time assembly, markdown. [Prod] postmortem regenerated after
resolution with human notes; edits by humans as a new version; a judge eval for narrative
quality (Phase 25). [Ent] templates per org, export to the incident tool, retention policy.
