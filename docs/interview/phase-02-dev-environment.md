# Interview prep — Phase 2: dev environment and shared contracts

## Q1. Why a uv workspace with shared libs, and not one package per service?
**30-second answer:** The services share contracts: events, findings and error format. I keep them in four small typed libraries in one uv workspace, with one lockfile, so every service uses the same version. The tradeoff is coupling: a breaking change in `libs/models` touches every service. I control that with `schema_version` on events and an ADR for breaking changes.

**2-minute answer:** Context: 10 Python services must speak the same language. Decision: a uv workspace with `libs/common`, `models`, `security` and `observability`, one `uv.lock`, and `mypy --strict`. How it works: services depend on libs as workspace members; CI runs `uv sync --frozen`, so the lockfile is the truth. What breaks: shared libs can turn into a "distributed monolith". Rule: libs hold contracts and cross-cutting helpers only, never business logic (libs/CLAUDE.md). At 10× scale: publish libs as versioned internal packages, so services can upgrade on their own schedule.

**Architecture:** `pyproject.toml` (virtual root), `libs/*/pyproject.toml`, `uv.lock`.

**Tradeoff (chosen on purpose):** a monorepo lockfile instead of independent versions, which is simpler now and less flexible later.

**Common mistake:** putting domain logic in "common" libs, so every service redeploys for one team's change.

**Follow-ups:** "How do you evolve an event schema?" → additive changes only, plus `schema_version`; consumers ignore unknown fields; breaking change = new event type. "Why not protobuf?" → it's an option at [Ent] for cross-language; Pydantic + OpenAPI cover Python + Java for now.

## Q2. How do you enforce "evidence-backed" output from an LLM?
**30-second answer:** I don't trust the prompt alone. The output schema makes it impossible: a Fact needs at least one evidence id, a HIGH-confidence hypothesis needs two distinct sources and no contradicting evidence, and a consequential recommendation must require approval. If the model breaks a rule, validation fails and we retry once or mark the step failed.

**2-minute answer:** Prompts are requests; schemas are rules. In `libs/models/findings.py`, Fact, Hypothesis and Recommendation are separate types joined in a discriminated union, so facts and guesses can't be mixed. Validators enforce the evidence rules. Phase 11 adds a second layer: a deterministic check that each cited id exists and that the user may see it. A third layer is an LLM judge for "does this evidence support this sentence". What breaks: the model can cite a real id that doesn't support the claim, and only the judge plus evals catch that.

**Architecture:** `libs/models/src/aeoi_models/findings.py`; tests in `libs/models/tests/test_models.py`.

**Tradeoff:** strict schemas cause more retries and higher cost, but give trust.

**Common mistake:** "we tell the model to cite sources" with no validation.

**Follow-ups:** "Confidence as a percentage?" → not until it is calibrated against labelled incidents (Phase 25). "What if no evidence exists?" → the report says INCONCLUSIVE and lists open questions.

## Q3. Why give AI tools a separate read-only database role?
**30-second answer:** Least privilege. Claude Code's Postgres MCP server, and later our agents, use `aeoi_readonly`: SELECT only, read-only transactions and a 15-second statement timeout. Even if the model is prompt-injected, it cannot change data. I tested that it cannot write even after it switches read-only mode off, because the real control is privileges, not the setting.

**Follow-ups:** "What about reading sensitive tables?" → Phase 3 grants schema usage table by table; identity and audit stay out. "In production?" → IAM auth, a separate replica endpoint, and row-level security where needed.

## Q4. Why is Kafka not in the default local profile?
**30-second answer:** Nothing uses it until Phase 18. Until then an in-process bus uses the same envelope. Starting a JVM broker every day costs RAM for no value. CI still starts Kafka on every push, so its configuration stays tested. Use technology when it has a job.
