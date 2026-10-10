# Interview prep — Phase 11: hypotheses, critic, validation

## Q1. Who decides the root cause in your system — the LLM?
**30-second answer:** No. Code proposes every candidate cause from typed evidence and gives it
a confidence band by a written rubric. One model ranks the candidates and explains them,
citing the evidence by reference; a second, stronger model attacks them. Code checks every
reference, recomputes the bands and validates the result before anything is stored.

**2-minute answer:** The evidence agents produce a timeline: a deploy at 09:42, latency up at
09:49, pool saturated at 09:50, errors at 09:51, traffic flat. Code turns that into candidates
with rules a human can read — a deploy can be a cause only if it started before the first
sustained symptom; a saturation that rose after every symptom is a consequence; a flat traffic
metric rules out a surge. The band is a rubric: HIGH needs three supporting observations from
two independent agents and nothing against it. The model can reorder and explain, but it
cannot add a cause, change a band, or cite something that isn't in the evidence — uncited
prose is dropped, not shown.

**Follow-up — "Then what does the LLM add?"** Ordering when the rubric ties, a readable
mechanism, and — from the critic — contradictions and alternatives that the rules did not
encode. Those alternatives are capped at MEDIUM and stay "proposed" until a human looks.

## Q2. How do you keep the critic independent?
**30-second answer:** Three ways. A different, stronger model: Haiku ranks, Sonnet critiques.
The critic gets the evidence and the candidates as code wrote them — never the ranker's
explanation or ranking. And a check fails the run if both ever end up on the same model.

**2-minute answer:** I strip the ranker's prose twice, in the orchestrator and in the critic
agent, and a test asserts a secret string from the ranker's explanation never reaches the
critic's prompt. The two agents rebuild the observation list independently, so I compare a
SHA-256 of it — same refs are not enough, they must mean the same thing. The critic runs with
fallback off: if Sonnet fails, the critique is missing and the run says PARTIAL. A quiet
fallback to a 3B local model would be labelled "critic" in every report and be worthless.

**Follow-up — "Why did you care so much about fallback?"** Because I found it the hard way:
the "reasoning" route had returned HTTP 400 since Phase 5 — the new Sonnet rejects the
`temperature` parameter — and nothing noticed, because fallback answered with llama and the
smoke test only called the fast route. Now the smoke calls every hosted route with fallback off.

## Q3. What did the review find?
**30-second answer:** Four serious defects, mostly about the CODE rules, not the model. Every
deploy before the symptom got HIGH, because its support included symptoms on unrelated
services. Background log noise could become "the first symptom" and rule out the real deploy.
A newline in an error code — log data — could forge an observation line the critic then cites.
And 13 ruled-out causes crashed the agent on a schema limit.

**2-minute answer:** Fixes: a deploy's support is its own service; other deploys before the
symptom count against it; only a sustained metric shift can time a cause out; error codes are
pattern-checked and every statement is one printable line. Two more I'm glad were caught:
alternatives and refuted candidates were stored as "validated", and the timing check could
never fail because support was built after the cause. And one is open, by choice: if any
evidence edge is missing, incident-service refuses the whole batch — nothing wrong is stored,
but the run loses its hypotheses; it's PARTIAL and documented.

## Q4. How do you know it's any good?
**30-second answer:** I don't, yet, and I say so. The tests prove the guarantees — citations,
independence, timing, permissions — with a fake model. Whether Haiku's explanations and
Sonnet's alternatives are good is a quality question that needs labelled incidents; the first
real runs on my Mac become those cases. No accuracy number until then.

**Follow-up — "Did a real run teach you anything?"** Yes, on the first one. Every check
passed, but Sonnet listed "traffic stayed flat" as evidence AGAINST the deploy — evidence that
actually rules out a rival — and my code trusted it, so the right answer dropped from HIGH to
LOW. Lesson: the model may point at evidence, but code decides what that evidence can mean
for each kind of cause. That run is now eval case #1 and a replay unit test.

## Weak spots to admit
- Rules are judgement, not calibrated; a cause no rule covers only appears as a critic
  alternative (MEDIUM at most).
- One critic pass, no judge, no loop for more evidence.
- Managers see code conclusions (deploy keys, metric names) but not evidence or model text.
- A paid Sonnet call can be retried on a 502/503; bounded by the budget and deadline.
