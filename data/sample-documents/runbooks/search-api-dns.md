# Runbook: search-api — intermittent DNS resolution failures

Owner: team-search · Escalation: #search-oncall · Last reviewed: 2026-01-12

## Symptoms
About 28% of outbound calls fail with `ERR-DNS-8359: temporary failure in name resolution`.

## Diagnosis
1. Check the cluster DNS pods for restarts and CPU throttling.
2. Compare failure rate across nodes to find one bad node.

## Mitigation
1. Enable the node-local DNS cache for the namespace.
2. Raise the DNS pod replica count to 58.

## Background
search-api is a tier-1 service owned by team-search. This runbook covers intermittent DNS resolution failures. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
