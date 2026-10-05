# ADR-007: Mandatory human-in-the-loop for consequential actions

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
AI output is probabilistic; production changes have blast radius; accountability needs a named human.

## Decision
Recommendations flagged `requires_approval` pause the graph (`interrupt`). Only INCIDENT_COMMANDER can approve. Approvals are single-use, expiring, bound to args hash, and fully recorded.

## Alternatives
| Option | Why not (here) |
|---|---|
| Fully autonomous remediation | Unacceptable risk; no accountability. |
| Advisory only, no action path | Safe but loses the controlled-execution demo. |

## Tradeoffs
Against: humans add latency and may rubber-stamp. Mitigation: show contradicting evidence and alternatives by default; track override rate; require a reason.

## Consequences
Approval UX is a core feature, not an afterthought.

## Prototype vs Production vs Enterprise-scale
[P] single approver. [Prod] two-person rule for SEV-1 prod changes. [Ent] policy-as-code (OPA) decides who may approve what.

## When we would revisit
Low-risk, reversible, well-evaluated actions could become auto-approved with a policy, never by default.
