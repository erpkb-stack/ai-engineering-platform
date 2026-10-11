# Interview prep — Phase 13: historical incident intelligence

## Q1. "We had this before" — how do you stop the system from just copying an old root cause?
- A past root cause never becomes evidence. It becomes a **base rate**: "2 of 3 similar past
  incidents had a deploy as root cause". Code attaches it to the candidate of that kind.
- It may only **break a tie inside a band**, after support and contradiction counts. It never
  moves a candidate across a band and never beats more evidence (mutation-tested).
- The ranker and the critic never see it, so it cannot anchor them.
- I first accepted "history raises the band", then rejected it: a band measures this
  incident's evidence. Inflating it with other incidents' outcomes is double counting.

## Q2. Past incidents are restricted per group. How do you not leak them?
- Retrieval: the group filter is in the SQL of **both** the keyword and the vector branch,
  groups come from the token, and a Python assert refuses any row outside them.
- Storage: the result is written on ANOTHER incident, which is visible per role. So the agent
  keeps only incidents shared with every engineer. Restricted hits are counted, never prompted,
  never stored, never used in a prior. A test plants one and checks it appears nowhere.
- Even shared ones are stored as pointers: key, category, date, ranks. No text.

## Q3. You use an LLM to compare incidents. Does it add anything?
Honestly, no — not today. Code computes every relation the model may claim (same service,
same error code, symptom present on both sides, deploy before). A claim survives only if code
agrees; one code cannot check is rejected. The owner chose "LLM compares"; I made it safe and
measurable (`claims_rejected`), and the ADR says the measurement decides whether it stays.

## Q4. Why hybrid search, and how do you know it helps?
Engineers describe symptoms in their own words ("the db client waits to borrow a connection");
the write-up says "connection acquisition timeouts". Vectors catch that; keywords catch exact
error codes. But I don't claim it helps until `make history-eval` runs on 36 held-out
paraphrases, and the report prints its own limits: one template per category, the error-code
variant is easy for keyword by construction, and I wrote the queries. Leakage must be 0.

## Q5. What happens when the embedding model is down?
Hybrid degrades to keyword and every hit says `retrieval: keyword` — also when no vectors exist
yet for the current model. Vector-only mode returns 503 instead of quietly answering with
keywords. Cosine between two models' vectors is never computed (`embedding_model` filter).

## Weak spots to admit
- The model step is redundant by design (Q3).
- `CATEGORY_KINDS` is a hand table over a synthetic taxonomy; the service boost weight is not
  tuned (no labelled data).
- The eval corpus is template-based; real postmortems would make it meaningful.
