# Interview prep — Phase 9: orchestration, checkpoints, delegation

## Q1. Why LangGraph, when you only have one agent?
**30-second answer:** For one agent the graph shape is ceremony, and I say so. What I needed
was the checkpointer: after every step the run state is in Postgres, so if the orchestrator
dies, the next start resumes at the step it was in. I tested it by killing the process with
`kill -9` in the middle of an agent call: it restarted, resumed, finished. Phase 10 adds
agents as more parallel branches on the same graph.

**2-minute answer:** The graph is `plan → fan-out per agent → collect evidence → finalize`,
compiled with LangGraph's Postgres saver. Two things I learned by testing, not by reading.
First, with a parallel fan-out, a branch that finished keeps its result when a sibling
crashes; only the crashed branch re-runs. Second, a node interrupted by a kill re-runs from
the start, so every node must be idempotent. Task ids are deterministic, inserts are
`ON CONFLICT DO NOTHING`, and before calling an agent the node checks whether that task's
execution is already recorded. One of my tests first passed with that guard removed — it
never reached the window where it matters — so I wrote one that kills the process between
the database write and the checkpoint. Ownership: I don't let the library create its tables;
they are in my Alembic migration, and a unit test fails if a library upgrade adds a migration.

**Follow-up — "What does resume NOT protect you from?"** A kill during the agent call repeats
the agent's work: the orphaned call finishes and is recorded, and the resumed run calls again.
I saw 4 tool calls instead of 2. It is visible (task attempt 2), not hidden; the real fix is a
task idempotency key the worker dedupes, which comes with Kafka.

## Q2. The investigation runs in the background. Whose permissions does it use?
**30-second answer:** The user's, through a delegation grant. When the user starts it, the
orchestrator trades their token once, at a token-exchange endpoint, for a grant bound to that
one investigation and incident. From the grant it gets five-minute tokens. Each refresh
re-reads the user's roles from the directory, so a deactivated user or a removed role stops
the run within one refresh. The user's own token is never stored.

**2-minute answer:** Shaped like OAuth token exchange (RFC 8693). The delegated token carries
the user, fresh roles, an actor claim (`act: service:orchestrator`), and the investigation and
incident ids. It is signed with a separate key, and it is accepted in exactly one place: the
on-behalf-of header next to an authenticated service token. As a normal bearer token it fails
everywhere, by key, issuer and audience, plus an explicit check. The tool gateway and the agent
worker refuse a call whose investigation or incident differs from the token's. That closed a
gap I documented in Phase 7: those ids used to be claims. Every grant decision — created,
issued, denied, revoked — is an append-only event row. The service that writes them cannot
update or delete them.

**Follow-up — "Why not just keep the user's JWT?"** It's a bearer credential at rest, valid
until its own expiry, for everything the user can do, and not revocable. The grant is scoped,
revocable, and re-checked.

## Q3. What did the security review find?
**30-second answer:** I had an independent read-only reviewer go through it. It found no
bypass, but 12 real defects; I fixed 11 with tests. The best one: I had revoked UPDATE and
DELETE on the history table, but a foreign-key cascade runs as the table owner, so deleting a
user would have erased their grant history anyway. Now the history has no foreign key at
all, and deleting a user with grants is refused.

**2-minute answer:** Others: the delegated token was bound to the investigation but not the
incident, so a token holder could write tool-call records into another incident's trace. The
trace endpoint showed raw log lines to roles that may read incidents but not logs, so now you
need the agent's permission to see prompts. A transient failure of the token exchange left a
failed row that "owned" the client's idempotency key, so every retry replayed the failure. I
had the incident marked INVESTIGATING before the last permission check, so a refused user
still changed the incident. Grants could stay live after a cancel/finalize race, so now I
revoke before I finish, and cancel always revokes. The one I did not fix: revocation stops new
tokens, not issued ones, so an in-flight agent call can keep acting for up to five minutes.
That is written in the ADR with the production fix: a revoked-grant check at the tool gateway.

## Q4. How do you know it works on a real machine, not just in tests?
**30-second answer:** Three levels. Integration tests with real Postgres, the real
checkpointer and least-privilege logins, where only the model is fake. A smoke script over the
real HTTP product path. And a live crash: I froze the agent process with SIGSTOP so the call was
guaranteed in flight, `kill -9`'d the orchestrator, restarted it, and it finished with task
attempt 2 and a refreshed token — no user involved.

**2-minute answer:** I also mutation-test the guards: remove the guard, the test must fail.
Five guards checked: execution reuse, revoke at finalize, the directory re-check, and the
tool-gateway and agent bindings. Running the whole stack live found bugs the in-process tests
could not: `make db-seed` would break after the first investigation, because the seed
truncates users and grants now reference them; and a status line printed local time with a
"Z", the same bug class I'd already fixed once in Phase 8. I don't quote latency numbers from
this phase: the sandbox runs used a fake model.

## Weak spots to admit
- One orchestrator process. Two replicas would both resume the same run; I documented the
  lease (`FOR UPDATE SKIP LOCKED`) or partition ownership that fixes it.
- The api acts as the token service here. In production that's the identity provider's
  token-exchange endpoint, and tokens should be sender-constrained (DPoP/mTLS).
- Knowledge tools refuse delegated calls until rag gets an on-behalf-of path (Phase 10).
- The revocation window above.
