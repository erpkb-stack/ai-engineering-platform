# Runbook: shipping-api — thread pool starvation

Owner: team-shipping · Escalation: #shipping-oncall · Last reviewed: 2026-07-11

## Symptoms
Latency rises while CPU stays below 40%. Logs show `ERR-THR-6514: task rejected from executor`.

## Diagnosis
1. Take a thread dump and count threads blocked on I/O.
2. Look for a synchronous call to a slow dependency inside a request handler.

## Mitigation
1. Increase the executor size from 13 to 46 as a temporary measure.
2. Move the slow call to an async client with a timeout.

## Background
shipping-api is a tier-1 service owned by team-shipping. This runbook covers thread pool starvation. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
