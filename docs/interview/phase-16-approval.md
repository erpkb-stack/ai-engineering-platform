# Interview prep — Phase 16: human approval, then controlled execution

## Q1. Walk me through how a rollback happens.
An investigation's code rule (top hypothesis = a validated deploy that is still running and has
an earlier release) proposes "roll back DEPLOY-X" FOR the engineer who started it - or an SRE
asks by hand. A second incident commander decides with a mandatory reason. If they approve,
the action runs inside that same request, as that person: a dedicated executor identity calls
the tool gateway with the approver's own token; the gateway asks incident-service - the system
of record - to verify and consume the approval, records its intent, runs the rollback through
an insert-only CD login, and records the result. The incident becomes MITIGATED.

## Q2. Why not LangGraph's interrupt, which your own ADR proposed?
Because a human can take an hour. An interrupt keeps the investigation running and its
delegated credential alive that whole time - or acts later with a 5-minute token that is
already dead. So the investigation ends with a pending request, and the decision brings its
own credential. I changed the ADR on purpose and wrote down why.

## Q3. What stops one person approving their own idea?
The requester is recorded - for an AI proposal it is the human who started the investigation,
not "the AI". The approver must be a different incident commander. That rule lives in the
service AND as a database CHECK, so a bug in the code still cannot store it.

## Q4. How do you know the approved action is the one that runs?
The approval is bound to a SHA-256 of the canonical arguments, the incident, and - once decided
- the approver. The gateway recomputes the hash from the VALIDATED input, so changing one
character of the reason is a different action and is refused. It is consumed in the same
transaction that checks it, and never after it expires.

## Q5. What did the independent review find, and what was the worst one?
Four high findings. The worst: my orchestrator recorded FAILED on any 4xx. A double click made
the second attempt return "in progress" (409) - recorded as FAILED - while the first attempt's
rollback actually ran. The record lied, and nothing could correct it. The fix separates "the
tool ran and failed" (recorded) from "this attempt was refused" (never recorded) and "unknown"
(retry is safe). Another one: an IC could call the tool directly in manual mode; the rollback
ran and nobody recorded the outcome. Now only the executor identity may run a consequential
tool. Each fix has a test that fails if the fix is removed - I checked by removing it.

## Q6. How do you make "exactly once" true, not just hoped for?
Layers, each tested: the approval is consumed once; the gateway checks its own record (an OK
row → "already executed"; a fresh unfinished intent → "in progress"); unique indexes allow one
intent and one success per approval; the CD key is derived from the approval and checked
(a taken key is refused, never reported as mine); a per-service lock stops two different
approvals from both passing the "still the running deploy" check.

## Q7. What can still go wrong?
The executor runs in the same process as the read agents - separate identity, same blast
radius for a code bug. The orchestrator, not the gateway, writes the outcome back; it relays
the gateway's answer with the tool call id, so they can be compared, but in production the
gateway should write it. MORE_INFO only records the question. The CD is simulated. And I have
no data on how often humans rubber-stamp - that is a Phase 25 metric (override rate).

## Q8. Why is the decision synchronous when your API rule says long work is 202?
It is short and bounded: api 15 s, orchestrator 12 s, executor 10 s, tool 8 s - each inside
the one outside it. If it does not answer in time the response says "result unknown - retry",
and the retry is safe by Q6. A 202 plus polling would add a status resource for seconds of work.
