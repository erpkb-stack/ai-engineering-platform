---
paths:
  - "services/orchestrator/**"
  - "services/agents/**"
  - "prompts/**"
---
# LangGraph & agent rules

- State is a typed Pydantic/TypedDict `InvestigationState`; nodes return partial updates only.
- Checkpoint every node (PostgresSaver) so an investigation resumes after a crash (Feature: checkpointing).
- Each agent has: `AGENT_VERSION`, a versioned prompt id (`prompts/<agent>/vN.md`), an input schema, an output schema, a max-tokens and max-tool-calls budget.
- Agents call tools ONLY via the Tool Gateway client and LLMs ONLY via the LLM Gateway client.
- Outputs are structured (`generate_structured`) and validated; on schema failure retry once with the validation error, then mark the node FAILED (do not loop).
- Findings must be one of: `Fact(evidence_ids>=1)`, `Hypothesis(supporting, contradicting, confidence_band)`, `Recommendation(requires_approval)`.
- Confidence is an **evidence-rubric band** (HIGH/MEDIUM/LOW), not an LLM-invented percentage, until calibration data exists (see ADR list).
- Deterministic work (timeline merge, metric anomaly maths, citation existence checks) is plain code, not an LLM call.
- The Critic must be given the evidence, not the previous agent's prose, and must produce at least one alternative or explicitly state why none exists.
