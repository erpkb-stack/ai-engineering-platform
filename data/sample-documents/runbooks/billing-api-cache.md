# Runbook: billing-api — cache stampede on cold start

Owner: team-billing · Escalation: #billing-oncall · Last reviewed: 2026-01-13

## Symptoms
After a cache flush, database load spikes and `ERR-CCH-4150: cache miss storm` appears in logs.

## Diagnosis
1. Check cache hit ratio for the last hour.
2. Look for many identical keys being rebuilt at the same time.

## Mitigation
1. Enable request coalescing so one request rebuilds a key.
2. Warm the top 32 keys before sending traffic; set TTL jitter to 45%.

## Background
billing-api is a tier-1 service owned by team-billing. This runbook covers cache stampede on cold start. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
