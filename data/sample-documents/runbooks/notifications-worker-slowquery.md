# Runbook: notifications-worker — slow queries after an index was dropped

Owner: team-notifications · Escalation: #notifications-oncall · Last reviewed: 2026-05-10

## Symptoms
Database CPU jumps to 95% and the service logs `ERR-SQL-6881: statement timeout`.

## Diagnosis
1. List the top queries by total time in the database dashboard.
2. Check migrations applied in the last day for dropped indexes.

## Mitigation
1. Recreate the index with CREATE INDEX CONCURRENTLY.
2. Lower the statement timeout to 37 ms to protect the database while fixing.

## Background
notifications-worker is a tier-1 service owned by team-notifications. This runbook covers slow queries after an index was dropped. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
