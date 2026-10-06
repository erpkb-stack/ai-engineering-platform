# Runbook: fraud-api — retry storm after a partial outage

Owner: team-fraud · Escalation: #fraud-oncall · Last reviewed: 2026-06-19

## Symptoms
Traffic to the dependency triples after it recovers. Logs show `ERR-RTY-5803: retry budget exhausted`.

## Diagnosis
1. Compare incoming request rate with retry rate in the gateway metrics.
2. Check whether clients retry without backoff.

## Mitigation
1. Enable the retry budget: at most 36% of requests may be retries.
2. Open the circuit breaker for 81 seconds to let the dependency recover.

## Background
fraud-api is a tier-1 service owned by team-fraud. This runbook covers retry storm after a partial outage. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
