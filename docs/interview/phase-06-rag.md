# Interview prep — Phase 6: permission-aware RAG

## Q1. How do you make sure RAG never shows a user a document they may not see?
**30-second answer:** The permission filter is inside the retrieval SQL, in every candidate branch, before the LIMIT. Each chunk carries a copy of its document's groups, kept in sync by a database trigger. The query says `allowed_groups && :caller_groups`. The caller's groups come only from the verified token, never from the request body. A restricted chunk is never even a candidate for an outsider. A test runs every restricted document against every persona without access, in all three search modes. It requires zero hits, and I mutation-tested it: remove the filter and the test fails.

**2-minute answer:** The common mistake is "retrieve top 40, then remove what the user can't see". That has two problems. First, a user with narrow access gets fewer than k results. Second, restricted text has already gone through your code, and maybe into a log or a prompt. So I filter in SQL. Four details make it hold up:
1. Chunk ACLs are maintained by a trigger: insert, and propagation when a document's groups change. Application code never sets them.
2. A Python assertion re-checks every returned row. That is defence in depth against a future query change.
3. Getting a document you can't see returns 404, not 403, because existence is information too.
4. A service token has no groups. So an agent must search with the user's token (on-behalf-of), or it sees nothing. That prevents the confused-deputy problem.

**Tradeoff:** with pgvector < 0.8, HNSW plus a selective filter can return fewer vector hits. Version 0.8 adds iterative scans, which I enable when I detect them.

**Follow-up — "What about the LLM seeing restricted text?"** The reranker sends RESTRICTED passages only to the local model route. That text never goes to a hosted provider.

## Q2. Why hybrid search, and why RRF?
**30-second answer:** Incident questions come in two kinds. Some contain an exact identifier, like an error code or a ticket number: keyword search wins there, and embeddings blur "2535" and "2534". Others describe the problem in different words: embeddings win there. RRF fuses the two lists by rank only, 1/(60+rank). Cosine similarity and ts_rank are on different scales, so adding the scores would need calibration per corpus. RRF needs none.

**The finding worth telling:** Postgres `ts_rank_cd` has no IDF. In my eval, the frequent word "tls" outranked the actual error code "2535". I added a boost for chunks that contain all the identifier-like terms of the query, as a stand-in for BM25. I wrote down that real BM25 (the ParadeDB extension) is the production answer if the eval keeps showing keyword misses.

**The better story — what the real run showed:** on my baseline, plain RRF hybrid was *worse*
than the better single branch for both query types (MRR 0.505 vs 0.662 for keyword alone). I
didn't tune by feel. I worked out the mechanism: RRF rewards agreement, and with deep candidate
lists "mediocre in both" beats "#1 in one" (2/90 > 1/61). Then I built a sweep that tunes fusion
on a dev split and reports on a held-out test split. Interviewers remember "hybrid made it worse,
and here is why" far longer than "hybrid is better".

## Q3. How do you know retrieval is good?
**30-second answer:** There is a versioned eval set with planted ground truth: 56 keyword queries, 52 paraphrase queries, plus leakage and injection checks. I report recall@1/5/10 and MRR@10 per search mode and per query type, with bootstrap confidence intervals. Every result file records the embedding model, the dataset hash, the pgvector version and the git commit. A run with fake embeddings is labelled "not a baseline". The CI uses that fake run only as a leakage and quarantine gate.

**Honest limit:** I wrote the eval set and the corpus myself, so it is biased toward my own phrasing. Real queries (with consent) are the real test. I would say that before the interviewer does.

**Follow-up — "Is the reranker worth it?"** On my hardware, no. A 3B local model hit the 25 s budget on every call, so the eval now aborts after three failures instead of wasting an hour. It stays off until a faster option (hosted small model or cross-encoder) shows a measured gain worth its latency.

## Q4. What happens at ingestion?
**30-second answer:** The pipeline is parse → redact secrets → scrub PII → prompt-injection tripwire → chunk → store → embed. The tripwire also scans text a human can't see, like `display:none` HTML. Hidden text is the classic indirect-injection carrier. A tripped document is quarantined with a reason: stored for review, never chunked, and also filtered at query time. A file without an explicit ACL is rejected. Chunks never cross section boundaries and start with "Title > Heading". Embedding is a separate, resumable step that commits per batch. Re-ingesting an unchanged file is a no-op, based on its sha256.

**Common mistake:** treating the injection detector as the defence. It is a tripwire with expected false negatives. The real controls are wrapping retrieved text as untrusted data, and tools that need authorization and human approval.

## Q5. What do you do when the embedding service is down?
Hybrid search degrades to keyword-only and the response says `degraded`. Vector-only search returns 503. The rag service's readiness check doesn't depend on the LLM gateway. The api gateway's readiness doesn't depend on rag either: search being down must not take incident management out of the load balancer.

## Resume bullet (only after your Mac run)
"Built permission-aware hybrid retrieval (pgvector HNSW + Postgres FTS, RRF) with ACL enforcement inside the SQL, prompt-injection quarantine and PII scrubbing at ingestion; zero leakage across a persona × mode test matrix; baseline recall@5 = <your number> on a 116-query eval."
Use the number from your committed `data/eval/results/` file, and never a number from a fake-embedding run.
