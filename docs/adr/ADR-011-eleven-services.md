# ADR-011: Eleven services from day one (instead of a modular monolith)

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
Spec lists 11 services; owner chose to keep them. One developer, Intel Mac.

## Decision
Build all 11 services, but each only in the phase that needs it, from a common template, with shared libs and compose profiles.

## Alternatives
| Option | Why not (here) |
|---|---|
| Modular monolith → extract later | Recommended by reviewer: same module boundaries, 1/5 of the ops cost; extract workers + Java + gateways when Kafka arrives. |

## Tradeoffs
Against (strong): distributed-systems cost before business value; more failure modes; local RAM pressure; interviewers may see it as resume-driven design. Interview answer: 'I would start with a modular monolith; I built services here deliberately to practise service boundaries, contracts and failure handling, and ADR-011 records that tradeoff.'

## Consequences
More CI time, more Dockerfiles, contract tests required.

## Prototype vs Production vs Enterprise-scale
[P] the cost is real. [Prod] boundaries match team ownership. [Ent] typical.

## When we would revisit
Phase 30 review: merge candidates are knowledge-service→service-catalog/rag, evaluation→offline job, audit→log pipeline.
