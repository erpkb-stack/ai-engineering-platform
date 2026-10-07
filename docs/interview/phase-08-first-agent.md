# Interview prep — Phase 8: the first agent

## Q1. What does your log agent actually use the LLM for?
**30-second answer:** Very little, on purpose. Code calls the log tool, groups the lines,
counts them and builds every fact: "203 ERROR lines with ERR_POOL_TIMEOUT, first seen
09:50:10Z". Each fact cites the exact log lines. The model gets the clusters and only gives
each one a short name and a category. If the model is down, the facts are the same and the
labels come from rules. I test that: same incident, model on and off, identical facts.

**2-minute answer:** Counting, times and error codes are exactly what small models get wrong,
and they're trivial for code. So I split the work. Code does the tool calls (oldest-first and
newest-first, so the onset and the most recent lines are both seen), the grouping by message
template, and the exact window counts, which come from SQL. Code also decides when it is allowed
to say "first seen": only when the oldest-first sample proves it. Otherwise it says
"earliest sampled". The LLM sees at most 8 clusters with 3 lines each, wrapped as untrusted data,
in one structured call. Its answer is validated: unknown cluster ids are dropped, and any cited
log line that wasn't in that cluster is dropped. I count what survives. That's my "label
quality" number, and it is how I compared the local model with Claude.

**Follow-up — "Then why have an LLM at all?"** A readable name and category turn 200 raw lines
into something an on-call engineer gets in one glance, and later agents (the hypothesis step,
the Critic) work on categories. But it is decoration on top of facts, never the facts.

## Q2. How do you stop the agent inventing evidence?
**30-second answer:** A fact can only cite evidence ids the tool gateway returned. The ids
contain the tool call that produced them (`LOG-<tool_call>-<n>`), so you can follow a claim to
the log line, to the tool call, to the audit event. Something with nothing to cite, like "no
error lines", is a note, not a fact. I removed a placeholder evidence id I had first written for
that case: a fake id is the start of fabricated evidence.

## Q3. Where is the trace, and who writes it?
**30-second answer:** The agent is stateless and returns its full trace: model, prompt version and
hash, tokens, cost, latency, every tool call. A thin orchestrator runner owns the tables. It
opens the investigation and task, calls the agent, stores the trace and the prompts it sent, posts
the cited evidence to the incident service, then closes the investigation. Every service writes
only its own schema. In Phase 9 LangGraph replaces the runner; the tables don't change.

**2-minute answer, the failure modes:** It's two services, so there's no single transaction. I
ordered the steps so every crash has a recovery. The trace is saved first. If the evidence POST
fails, the investigation is FAILED and the batch is kept, so a `repost-evidence` command retries
it, idempotently. A review caught that my first docstring said "just re-run", which is wrong: a
re-run makes new tool calls with new evidence ids. Exceptions and Ctrl-C mark the task and
investigation FAILED. A hard kill leaves RUNNING rows, and the next run cancels anything older
than twice the deadline. A unique index allows only one RUNNING investigation per incident.

## Q4. Local model vs Claude — which is better?
**Honest answer:** I can tell you what I measured on one incident: latency, tokens, cost, schema
repairs, how many labels passed validation and how many citations were dropped. That's n=1, so
it's an anecdote and I label it that way in the result file. The defensible conclusion is
structural: the facts were identical across models because models don't make facts. Which model
names clusters better needs a labelled eval set, and that's a later phase.

## Weak spots to admit
- One labelling call covers all clusters, so injected text in one cluster can sway another's
  label. I flag it in `degraded` instead of claiming isolation.
- A holder of `evidence:write` could still post plausible fake rows. The checks are a hash and
  that the key matches the tool call; production would verify against the tool-call record.
- The per-investigation dollar budget is enforced by the LLM gateway's cap, not per task yet.
