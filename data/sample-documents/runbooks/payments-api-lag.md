# Runbook: payments-api — Kafka consumer lag

Owner: team-payments · Escalation: #payments-oncall · Last reviewed: 2026-01-10

## Symptoms
Consumer group lag grows above 28 messages and downstream data is stale. The consumer logs `ERR-LAG-2424` on rebalance.

## Diagnosis
1. Check the number of consumers versus partitions.
2. Look for a poison message that is retried forever.
3. Check processing time per message on the dashboard.

## Mitigation
1. Scale consumers up to the partition count.
2. Move the poison message to the dead-letter topic with the replay tool.
3. Raise `max.poll.interval.ms` to 68 only if processing is legitimately slow.

## Background
payments-api is a tier-1 service owned by team-payments. This runbook covers Kafka consumer lag. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
