# Runbook: ledger-worker — bad feature flag rollout

Owner: team-ledger · Escalation: #ledger-oncall · Last reviewed: 2026-07-11

## Symptoms
Errors start seconds after a flag change. Logs show `ERR-FLG-8527: invalid configuration value`.

## Diagnosis
1. Check the flag audit log for changes in the last 30 minutes.
2. Compare error rate by flag cohort.

## Mitigation
1. Turn the flag off for all cohorts.
2. Add a validation rule so the value must be between 27 and 48.

## Background
ledger-worker is a tier-1 service owned by team-ledger. This runbook covers bad feature flag rollout. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
