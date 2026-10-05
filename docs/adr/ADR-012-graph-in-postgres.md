# ADR-012: Postgres edges + recursive CTE for the knowledge graph (no graph DB)

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
Feature 9 needs service→repo/API/DB/pipeline/runbook relationships and impact analysis.

## Decision
Store edges in `catalog.service_dependencies` and `knowledge.edges(src_type, src_id, rel, dst_type, dst_id)`; recursive CTE with a depth limit (≤4) and cycle guard.

## Alternatives
| Option | Why not (here) |
|---|---|
| Neo4j / Neptune | Better for deep, variable-length traversal and graph algorithms at 10⁶+ edges; extra system to run and sync. |

## Tradeoffs
Against: CTEs get slow for deep, wide traversals; no graph algorithms. Mitigation: depth limits, materialised closure table for hot queries.

## Consequences
Fast enough for 100–10k services; one datastore.

## Prototype vs Production vs Enterprise-scale
[P]/[Prod] Postgres. [Ent] graph DB if path queries over millions of edges or graph ML are needed.

## When we would revisit
p95 impact query > 500 ms, or a need for centrality/community detection.
