# Runbook: pricing-api — upstream rate limiting (HTTP 429)

Owner: team-pricing · Escalation: #pricing-oncall · Last reviewed: 2026-03-13

## Symptoms
Upstream responds HTTP 429 and the service logs `ERR-RTL-7924: quota exceeded`. Retries make it worse.

## Diagnosis
1. Check the retry count per request in traces.
2. Confirm the current quota with the upstream team.

## Mitigation
1. Switch retries to exponential backoff with jitter, capped at 20 attempts.
2. Request a temporary quota increase to 58 requests per second.

## Background
pricing-api is a tier-1 service owned by team-pricing. This runbook covers upstream rate limiting (HTTP 429). It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
