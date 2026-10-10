# Interview prep — Phase 12: timeline + postmortem

## Q1. You let an LLM write postmortems. How do you stop it making things up?
**30-second answer:** The model writes sentences, nothing else. Code builds the timeline,
computes every number in the header, and copies the root cause verbatim from the validated
hypothesis. Each model sentence must cite observation ids, and code drops a sentence that
cites nothing, contains a link, or contains a number or date that is not in the facts it
cites. The drops are counted and shown on the page.

**2-minute answer:** The writer gets the observations with their times, the validated
hypotheses, and a small block of numbers code computed — "7 minutes from deploy to first
symptom". So it never needs to compute. The number check works on tokens: "900ms" and "3x"
count as numbers, "5xx" is a name because it appears in the input, and dates are taken out
first so "12 minutes" is not "known" just because some date ends in 12. It is a heuristic:
it stops invented quantities, not a wrong claim built from real numbers. That is what the
eval set in Phase 25 is for.

**Follow-up — "Why not let the model compute the durations?"** Because then a wrong
subtraction looks exactly like a right one. Code computes them once, from that run's own
timeline points, and the page says "computed by code".

## Q2. How is the postmortem permission-aware?
**30-second answer:** It's assembled when you read it, per reader. Timeline rows whose source
is LOG or METRIC need that permission, filtered in SQL. A model sentence is shown only if you
may read every piece of evidence it cites — otherwise the text, its refs and its evidence ids
are all removed. The root cause is a code conclusion, visible to every incident reader, same
as the hypotheses API.

**2-minute answer:** Two leaks the review found are worth telling. First, the "did not
change" lines — "CPU stayed within its baseline, median 0.45" — were code-written, so I had
shown them to everyone. But they ARE metric data, so now they carry evidence ids and are
redacted like sentences. Second, the timeline row's source decided its visibility, and the
caller set it. Now incident-service derives it from the stored evidence kind and refuses a
point whose evidence needs two different permissions.

## Q3. Why assemble at read time instead of storing the document?
**30-second answer:** Two reasons. Freshness: status, resolution time and human timeline
events change after the investigation; a stored document is stale the moment someone
resolves the incident. And visibility: one stored document can't be right for an SRE and a
manager at the same time.

## Q4. What broke that tests didn't catch?
**30-second answer:** Time zones. The live run showed "04:42Z" for a 09:42 deploy: the DB
session returned -05:00 offsets, and I formatted with a literal "Z". The same bug made the
dedupe key differ for the same instant, so a resent point was inserted twice — the
end-to-end test caught that one. Both are now normalised to UTC, with tests that use a
-05:00 offset on purpose.

## Weak spots to admit
- A sentence is redacted by what it cites, but the model saw everything; latent today (no
  role has partial evidence permissions), documented.
- The number check is a heuristic; narrative quality is unmeasured until Phase 25.
- One more Sonnet call per investigation; the root cause stays "not confirmed" until Phase 16.
