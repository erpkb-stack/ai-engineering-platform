# Interview prep — Phase 10: multi-agent investigation

## Q1. You built a "multi-agent" system where three of four agents don't use an LLM. Why call them agents?
**30-second answer:** Because "agent" here means an isolated worker with its own narrow tool
allow-list, budget, trace and version, not "something that calls a model". Metrics, deployment
and knowledge do arithmetic and lookups: anomaly onsets, deploy timing, retrieval. A model would
only reword numbers that code already got right, and on my laptop's CPU each model call costs a
minute. The model belongs in the next phase, where it ranks hypotheses over this evidence.

**2-minute answer:** The architecture doc said parallel agents cut wall-clock time. I checked
that against my machine: local Ollama on an Intel CPU answers one call at a time, so four LLM
agents in parallel are four calls in a queue. The claim only holds for I/O-bound work. So I made
the new agents code-only; they finish in under a second while the log agent waits for its model.
What multi-agent still buys: least privilege (the metrics worker can only call `query_metrics`,
enforced by the gateway, not by a prompt), independent failure (one agent failing gives PARTIAL,
not FAILED), and per-agent tests and versions.

**Follow-up — "When would you add an LLM to the metrics agent?"** When it must decide something
code can't: say, which dependency's metrics to pull next. Not to write "latency went up".

## Q2. How do you detect a metric anomaly, and how do you know the facts are true?
**30-second answer:** A robust z-score against the same series' baseline before the incident
window: median and MAD instead of mean and standard deviation, because one spike in the baseline
inflates sigma and hides the real change; I have a test for exactly that. A change must last
three buckets and still be going to be called a shift.

**2-minute answer:** The independent review found three ways my facts could be false, and they
were all real. A series with only single-bucket spikes was reported as "stayed within its
baseline, max z 302 < 4". A short run at the window end was called "back within baseline". And a
count that is usually zero had MAD zero, so 0.2 errors per minute became z = 200,000; now each
metric has an absolute floor. I also state negative facts on purpose: "requests per second did
not move" is what rules out a traffic surge later. On the demo data the agents give the order
latency 09:49, pool 09:50, errors 09:51, threads 09:52, and the deploy at 09:42. That ordering
is the evidence a critic needs.

**Follow-up — "Are thresholds calibrated?"** No. z ≥ 4 and three buckets are judgement; I say so
in the ADR. Calibration needs labelled incidents (Phase 25) and seasonal baselines.

## Q3. The knowledge agent stores only document IDs. Isn't that useless?
**30-second answer:** It's the only safe option. Retrieval is filtered by the user's groups in
SQL, but the investigation's results are stored on the incident, and everyone who can read the
incident sees them. If I stored the runbook text, a document restricted to the security team would
leak to every incident reader. So I store a pointer, and each reader opens it through rag with
their own token.

**2-minute answer:** This came with making rag accept the investigation's delegated token at
all. In Phase 9 rag verified the user as the primary bearer, and delegated tokens are only
allowed in the on-behalf-of slot, so knowledge tools refused them. Now the tool gateway calls rag
with its own service token, scoped `rag:obo`, plus the user's or the delegated token in the
on-behalf-of header. rag takes the groups from that user, never from the service. A service token
alone gets 403: no confused deputy. And the delegated token can only search, not open whole
documents, because the investigation never needs that.

**Follow-up — "What still leaks?"** Existence: someone with docs:read outside the groups can see
that a document id matched. No title, no text. Production fix: filter references per reader.

## Q4. What happens when one of the four agents fails or hangs?
**30-second answer:** Each parallel branch records its own result. If some fail, the run is
PARTIAL and the error names the missing sources; the good evidence is kept. COMPLETE would hide
a missing source: "the deploy agent found nothing" is not "the deploy agent didn't run".

**2-minute answer:** The review found a gap: if the investigation deadline hit while one agent
hung, the whole run went FAILED and the three finished agents' evidence was never posted. Now the
deadline path posts the recorded evidence of the finished tasks, marks the hung one TIMED_OUT and
finishes PARTIAL; I mutation-checked that test. For crashes, the finished siblings are not called
again after a resume. I was precise about why: it's LangGraph's pending writes, not my reuse guard.
Removing the guard did not fail the 4-agent test, so I documented that and kept the Phase 9 test
that does pin the guard.

## Q5. What did you find only by running it?
**30-second answer:** The gateway's PII scrubber turned a digit-only UUID into "[CARD]…": the
document id looked like a credit card number. The knowledge agent then crashed parsing it. Unit
tests with fake tools couldn't see it; the in-process integration run did. Canonical UUIDs now skip
the scrubber, with a test that the scrubber still catches real card numbers next to them.

**Follow-up — "and a security bug you hadn't designed for?"** Evidence visibility. Since Phase 8,
anyone who could read an incident could read its raw log lines through the evidence endpoint,
including managers without log access. I had fixed the same leak for the trace in Phase 9 and
missed this endpoint. Now each evidence kind needs the permission of the tool that produced it,
filtered in SQL.

## Weak spots to admit
- No speed-up on my laptop: the run waits for the log agent's model call.
- Fixed thresholds, no seasonality; a blind sweep of nine metrics per service (a catalog would
  tell which ones exist).
- Rate limits: two investigations by one user in a minute can hit the metrics burst; the result
  says which series were skipped.
- Revocation window from Phase 9 (≤ 5 min) still applies to every agent.
