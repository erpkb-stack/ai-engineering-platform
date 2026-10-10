"""agents settings (env prefix AEOI_AGENTS_)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class LogAgentBudget(BaseServiceSettings):
    """Hard limits per task. Exceeding one degrades the result; it never loops."""

    model_config = SettingsConfigDict(env_prefix="AEOI_AGENTS_LOG_", extra="ignore", frozen=True)

    route: str = "fast"  # prompt front matter suggests; this decides (and a task may override)
    max_tool_calls: int = 6  # 2 per service (oldest-first + newest-first), 3 services max
    lines_per_call: int = 150
    max_clusters_for_llm: int = 6
    lines_per_cluster_in_prompt: int = 2
    line_chars_in_prompt: int = 200
    # Mac run: prompt v1 timed out at 90 s on Intel-CPU llama3.2:3b. v2 sends less input and asks
    # for much less output (no 40-char ids, no summary). 115 s < the gateway request cap (120 s).
    llm_timeout_s: float = 115.0
    llm_max_tokens: int = 400
    max_facts: int = 10
    # whole task (tools + LLM) must end before the orchestrator's HTTP timeout (180 s)
    task_deadline_s: float = 150.0
    min_time_for_llm_s: float = 10.0


class MetricsBudget(BaseServiceSettings):
    """Metrics agent (code only, no LLM - ADR-020)."""

    model_config = SettingsConfigDict(
        env_prefix="AEOI_AGENTS_METRICS_", extra="ignore", frozen=True
    )

    # 9 metrics x 2 services; the gateway burst is 20 per (user, tool) - stay below it
    max_tool_calls: int = 18
    baseline_min: int = 120  # baseline = this many minutes BEFORE the window
    max_points: int = 300  # 1-minute buckets for a <= 5 h query
    z_threshold: float = 4.0
    min_shift_buckets: int = 3
    max_facts: int = 20
    task_deadline_s: float = 60.0


class DeploymentBudget(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_AGENTS_DEPLOY_", extra="ignore", frozen=True)

    lookback_h: int = 6  # a deploy hours before the window can still be the cause
    max_deploys_per_service: int = 5
    max_config_diffs: int = 6
    task_deadline_s: float = 60.0


class KnowledgeBudget(BaseServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="AEOI_AGENTS_KNOWLEDGE_", extra="ignore", frozen=True
    )

    runbooks_k: int = 3
    docs_k: int = 3
    task_deadline_s: float = 60.0


class ReasoningBudget(BaseServiceSettings):
    """Hypothesis + critic agents (ADR-021). Owner decision (2026-10-09): Haiku ranks (`fast`),
    a STRONGER, different model critiques (`reasoning` = Sonnet) - the critic must be
    independent of the author (architecture.md §9 reason 4)."""

    model_config = SettingsConfigDict(
        env_prefix="AEOI_AGENTS_REASONING_", extra="ignore", frozen=True
    )

    rank_route: str = "fast"
    critic_route: str = "reasoning"
    # never critique on a fallback model: a 3B llama "critic" would look like Sonnet in every
    # report (found on the Mac: the reasoning route had silently failed since Phase 5)
    critic_allow_fallback: bool = False
    rank_max_tokens: int = 900
    critic_max_tokens: int = 1200
    # Haiku answers in seconds; the llama fallback on an Intel CPU needs the long timeout
    llm_timeout_s: float = 115.0
    task_deadline_s: float = 150.0  # below the orchestrator's 180 s HTTP timeout


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_AGENTS_", extra="ignore", frozen=True)

    service_name: str = "agents"
    port: int = 8003
    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    # ADR-019: delegated tokens are accepted in the on-behalf-of slot only if this file exists
    delegation_public_key_file: Path = Field(
        default_factory=lambda: _secret("delegation_public.pem")
    )
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    tool_gateway_url: str = "http://localhost:8006"
    llm_gateway_url: str = "http://localhost:8005"
    # ONE service identity for this worker: scopes tools:invoke + llm:invoke (make agents-tokens)
    service_token_file: Path = Field(default_factory=lambda: _secret("agents_service_token.txt"))
