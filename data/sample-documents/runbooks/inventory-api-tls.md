# Runbook: inventory-api — TLS certificate expiry

Owner: team-inventory · Escalation: #inventory-oncall · Last reviewed: 2026-01-18

## Symptoms
Clients fail the handshake with `ERR-TLS-2535: certificate has expired`. The failure started at midnight UTC on the expiry date.

## Diagnosis
1. Run the certificate inventory report for the service.
2. Check whether the auto-renewal job ran in the last {a} days.

## Mitigation
1. Issue an emergency certificate through the internal CA portal.
2. Restart the ingress pods so they load the new certificate.
3. Add an alert 55 days before expiry.

## Background
inventory-api is a tier-1 service owned by team-inventory. This runbook covers TLS certificate expiry. It was written after previous incidents where the first responder lost time looking at the wrong dashboard. Start with the symptom checks above; do not restart everything at once, because a full restart hides the evidence you need for the postmortem.

## Verification
Error rate returns to the baseline for 15 minutes and the alert resolves on its own.
