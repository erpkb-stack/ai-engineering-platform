# Runbook: checkout-api — database connection pool exhaustion

Owner: team-checkout · Escalation: #checkout-oncall · Last reviewed: 2026-05-13

## Symptoms
HTTP 500 responses rise sharply and request latency climbs. Logs show `ERR-POOL-2824: timed out after 30000 ms waiting for a connection from the pool`.

## Diagnosis
1. Open the service dashboard and compare active connections with `maxPoolSize`.
2. Check whether a deployment happened in the last two hours.
3. Look for code paths that acquire one connection per item in a loop.

## Mitigation
1. Roll back the most recent deployment if it correlates with the spike.
2. As a stop-gap, raise `maxPoolSize` from 10 to 88 and restart pods one by one.
3. Add an alert when pool utilisation stays above 90% for 5 minutes.

## Background
checkout-api is a tier-1 service owned by team-checkout. This runbook covers database connection pool exhaustion. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
