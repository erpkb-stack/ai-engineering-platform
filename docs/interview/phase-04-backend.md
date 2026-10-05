# Interview prep — Phase 4: backend services

## Q1. How do you guarantee idempotency?
**30-second answer:** Every create carries an Idempotency-Key. I store the key, a hash of the request body, and the response, in Postgres, in the **same transaction** as the incident. A retry gets the stored response back. The same key with a different body gets 422. Keys are scoped per user and per endpoint. If two identical requests race, the primary key makes one of them lose; its whole transaction rolls back, and it replays the winner's result.

**2-minute answer:** The context is alert webhooks and humans, who both retry. My first decision was where to store the key. Redis-only fails the atomicity test: a crash between "incident committed" and "key saved" means a retry creates a duplicate. So the key lives in the same database transaction as the incident. The race is handled by the database, not by locks in my code: insert, catch the unique violation, retry once, and replay. My test sends 6 identical requests at the same time and gets 1 incident; the logs show the other 5 replayed. At scale, Redis goes in front as a fast cache for replays, and a TTL job cleans up old keys (it uses the `expires_at` index).

**Tradeoff:** one extra write per POST. I chose correctness over a few milliseconds.

**Common mistakes:** check-then-insert without a unique constraint (it races); a key that isn't scoped to the user (another user can replay your response); returning a replay for a *different* body.

**Follow-ups:**
- "What about PATCH?" PATCH uses optimistic locking with If-Match instead: a stale writer gets 412.
- "And the event?" The event is in the outbox in the same transaction, so a replay never emits a second event.

## Q2. Why a transactional outbox instead of publishing to Kafka directly?
**30-second answer:** The dual-write problem: "commit to the DB, then publish" loses the event if the process dies in between, and "publish, then commit" announces something that never happened. The outbox writes the event as a row in the same transaction as the data. A relay publishes it later. Delivery is at-least-once, so consumers dedupe on event_id. I don't claim exactly-once.

**Follow-up — "How do several relays avoid double publishing?"** `SELECT … FOR UPDATE SKIP LOCKED`. My test runs two relays at the same time over 20 events and checks that no event went to both. Per-incident ordering: a relay stops the batch at the first failure, so later events never overtake earlier ones.

**Honest correction to mention:** my first plan used an in-process event bus. That cannot connect separate services. I caught it when building the second service and replaced it with the outbox. Interviewers like hearing that you corrected a design.

## Q3. How is the API secured?
**30-second answer:**
1. RS256 JWT with a pinned algorithm. Tests cover `alg=none` and HS256 key confusion.
2. Identity only from the verified token, never from the body.
3. Permissions checked at the gateway (cheap; unauthorised calls never reach a service) **and again** in the service.
4. Each service connects to Postgres as its own login, which can touch only its own schema.

**Follow-up — "Why check twice?"** A misrouted internal call, a new route someone forgot to guard at the gateway, or an attacker inside the network. Authorisation lives where the data lives.

## Q4. "Tell me about a bug your tests found."
**Answer:** The default structured-logging traceback renderer included every stack frame's local variables. Any exception could write a bearer token or a request body into the logs. A test that raises inside a function holding a fake token caught it. I disabled locals and kept the test. Logs are a common data-leak path; they get shipped to third-party log tools with wide read access.

## Q5. Why keyset pagination instead of OFFSET?
**30-second answer:** OFFSET gets slower the deeper you page, and it skips or repeats rows when new incidents arrive while you are paging. Keyset (`WHERE (created_at, id) < cursor`) uses an index, costs the same on every page, and is stable under inserts. The test pages through 5 incidents 2 at a time and checks there are no gaps and no duplicates.
