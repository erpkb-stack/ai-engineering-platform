# Runbook: orders-api — memory leak and OOMKilled pods

Owner: team-orders · Escalation: #orders-oncall · Last reviewed: 2026-02-18

## Symptoms
Pods restart every few hours with reason OOMKilled. Heap graphs grow in a saw-tooth pattern. Error code `ERR-MEM-4657` appears just before each restart.

## Diagnosis
1. Compare heap usage before and after the last release.
2. Capture a heap dump from one pod before it is killed.
3. Check for unbounded in-memory caches keyed by request id.

## Mitigation
1. Lower traffic to the pod group by 14% using the traffic split.
2. Set the cache size limit to 88 entries and redeploy.
3. Open a ticket to add a memory regression test.

## Background
orders-api is a tier-1 service owned by team-orders. This runbook covers memory leak and OOMKilled pods. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
