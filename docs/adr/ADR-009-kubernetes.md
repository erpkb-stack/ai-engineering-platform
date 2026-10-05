# ADR-009: Kubernetes for deployment (kind locally, EKS reference)

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
Agent workers must scale independently; zero-downtime deploys; standard enterprise target.

## Decision
Kustomize base + overlays; kind for local/CI; EKS as production reference (not provisioned).

## Alternatives
| Option | Why not (here) |
|---|---|
| Compose only | Fine for [P]; no autoscaling/probes story. |
| ECS/Cloud Run | Simpler managed options; valid production answer for a small team. |

## Tradeoffs
Against: big complexity tax for a solo project; kind on an Intel Mac is RAM-hungry. Mitigation: K8s only from Phase 22, compose remains the daily driver.

## Consequences
Need manifests, probes, HPA/KEDA, NetworkPolicies.

## Prototype vs Production vs Enterprise-scale
[P] kind. [Prod] EKS + managed data services. [Ent] multi-cluster, GitOps (Argo CD).

## When we would revisit
If the team is small and workloads simple, prefer ECS/Cloud Run.
