# Runbook: cart-api — disk full on the log volume

Owner: team-cart · Escalation: #cart-oncall · Last reviewed: 2026-07-13

## Symptoms
Writes fail with `ERR-DSK-4257: no space left on device` on the log volume. The service stops accepting requests when it cannot write its audit log.

## Diagnosis
1. Check volume usage on the node dashboard.
2. Find which log files grew fastest in the last hour.

## Mitigation
1. Delete rotated logs older than 32 days from the volume.
2. Lower the log level from DEBUG to INFO and redeploy.
3. Increase the volume to 82 GiB in the next change window.

## Background
cart-api is a tier-1 service owned by team-cart. This runbook covers disk full on the log volume. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
