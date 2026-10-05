# ADR-006: Permission-aware retrieval enforced in SQL

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
Documents have different audiences. LLMs cannot be trusted to hide content they have seen.

## Decision
Each chunk stores `allowed_groups[]`; the retrieval SQL applies `allowed_groups && :user_groups` in the same statement as vector/FTS search; groups come from the verified JWT. Tests assert zero leakage at the repository layer.

## Alternatives
| Option | Why not (here) |
|---|---|
| Post-filter in app | Leaks through counts/latency, breaks top-k, easy to bypass. |
| Ask LLM to hide | Not a control. Prompt injection defeats it. |
| Separate index per group | Explosion of indexes; good for a few high-sensitivity tiers only. |

## Tradeoffs
Against: ACL changes require re-tagging chunks; filtered ANN recall drop. Mitigation: ACL by group not user; sync job; iterative scan.

## Consequences
Every ingestion path must set ACLs. Missing ACL ⇒ default deny.

## Prototype vs Production vs Enterprise-scale
[P] groups in JWT. [Prod] groups from IdP + source-system ACL sync. [Ent] ReBAC (e.g. OpenFGA/Zanzibar-style) for fine-grained sharing.

## When we would revisit
If ACLs become per-user/per-document dynamic → ReBAC service.
