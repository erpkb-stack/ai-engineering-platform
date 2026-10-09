"""orchestrator settings (env prefix AEOI_ORCH_)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import make_url

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root, service_database_url
from aeoi_models.api.agents import AgentName


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_ORCH_", extra="ignore", frozen=True)

    service_name: str = "orchestrator"
    port: int = 8002
    db_user: str = "orch_svc"
    db_password_file: Path = Field(default_factory=lambda: _secret("orch_svc_password.txt"))
    db_url_override: SecretStr | None = None
    db_pool_size: int = 4
    checkpoint_pool_size: int = 4

    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    incident_service_url: str = "http://localhost:8001"
    agents_url: str = "http://localhost:8003"
    api_url: str = "http://localhost:8000"  # token exchange (STS) lives in the api, ADR-019
    # scopes agents:run + evidence:write + delegation:create (make agents-tokens)
    service_token_file: Path = Field(
        default_factory=lambda: _secret("orchestrator_service_token.txt")
    )
    # look-back/look-ahead around detected_at when the caller gives no window
    window_before_min: int = 60
    window_after_min: int = 30
    budget_usd: Decimal = Decimal("0.50")
    # Phase 10 (ADR-020): the agents every investigation runs, in parallel (Send per agent)
    agents: tuple[AgentName, ...] = ("log_analysis", "metrics", "deployment", "knowledge")
    task_deadline_s: float = 180.0  # > agents' task deadline (150 s): one agent call
    # the whole investigation, all retries and resumes included; past it = FAILED
    investigation_deadline_s: float = 900.0
    # refresh the delegated token when it has less than this left (tokens live 300 s)
    token_refresh_margin_s: float = 60.0
    # Ollama answers one request at a time on the Mac: more would only queue and time out
    max_concurrent_investigations: int = 2
    resume_on_startup: bool = True
    http_timeout_s: float = 10.0  # everything except the agent call itself

    def sqlalchemy_url(self) -> str:
        if self.db_url_override is not None:
            return self.db_url_override.get_secret_value()
        return service_database_url(
            user=self.db_user, password_file=self.db_password_file
        ).render_as_string(hide_password=False)

    def libpq_url(self) -> str:
        """Plain postgresql:// DSN for psycopg (LangGraph's checkpointer)."""
        return (
            make_url(self.sqlalchemy_url())
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
