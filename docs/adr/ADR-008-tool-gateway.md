# ADR-008: Tool Gateway as the only path from agents to systems

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
Agents are non-deterministic and can be prompt-injected; tools reach real systems.

## Decision
All tool access goes through `services/tool-gateway`: schema validation, user∩agent authorization, policy, timeouts/retries/breakers, output sanitisation, evidence ids, audit.

## Alternatives
| Option | Why not (here) |
|---|---|
| Tools as in-process functions in agents | Simpler, but each agent then holds credentials and the policy is scattered. |
| MCP servers called directly by agents | Good protocol for tool definitions; still needs a central policy/audit point. The gateway can expose/consume MCP. |

## Tradeoffs
Against: extra hop latency and a single point of failure. Mitigation: horizontal scaling, per-tool breakers, cache READ results.

## Consequences
Credentials live only in the gateway. Every tool needs a full contract (`/new-tool` skill).

## Prototype vs Production vs Enterprise-scale
[P] one replica. [Prod] HA + egress proxy. [Ent] per-domain gateways with shared policy engine.

## When we would revisit
Never removed; may be split by domain.
