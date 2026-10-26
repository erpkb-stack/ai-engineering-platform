# Phase 27 - Load testing: interview notes (ADR-039)

Measured on one Intel Mac (kind, 1 replica per service, each Python service capped at 1 CPU),
fake LLMs waiting the measured per-agent p50 of real calls. Files: `data/load/results/`.

## Questions
1. How do you load test an LLM system without paying for the LLM?
2. What was your capacity?
3. Your rate limiter: did it work under load?
4. What did you get wrong?

## 30-second answers
1. Fakes that WAIT: each agent's fake call waits the p50 of that agent's real calls, read from
   the gateway's own usage table. Refused unless every provider is fake, so it can never slow
   or pass for a real call. Check: the fake investigation ran 43.5 s, the real paid run 40.8 s.
2. Reads bend between 10 and 25 rps on the laptop - and two identical runs differed ~10x, so I
   do not quote one number. Investigations: ~2.8 per minute = 2 orchestrator slots / 43.5 s,
   exactly as modelled; above it the queue grows (p50 20 s at 4/min).
3. It never engaged: 0 % 429. The system bent before the team's policy cap (~62 rps), and
   overload showed up as slow 503s - so the next work is load SHEDDING, not a bigger limit.
4. Three things. I predicted 429s from 62 rps - wrong, the system bent first. I tried to prove
   "the api hits its 1-CPU limit" with `ps %CPU`, which is a lifetime average - wrong tool; the
   right one is the cgroup throttling counters, not run, so the bottleneck stays unproven. And
   my first load script started 100 investigations instead of 12 (Locust splits users across
   classes) - caught in a sandbox dry run, plus the backlog it left, which the tool now refuses.
