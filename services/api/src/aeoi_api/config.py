from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root, service_database_url


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_API_", extra="ignore", frozen=True)

    service_name: str = "api"
    port: int = 8000
    incident_service_url: str = "http://localhost:8001"
    rag_service_url: str = "http://localhost:8004"
    tool_gateway_url: str = "http://localhost:8006"
    orchestrator_url: str = "http://localhost:8002"
    audit_service_url: str = "http://localhost:8008"
    upstream_timeout_s: float = 5.0
    # search may include an LLM rerank (25 s budget in rag) - a longer, explicit timeout
    rag_timeout_s: float = 35.0
    # longest tool deadline (10 s, rag-backed tools) + recording
    tools_timeout_s: float = 15.0
    # orchestrator start does: read incident + mark INVESTIGATING + token exchange (all fast);
    # the investigation itself runs in the background (202)
    orchestrator_timeout_s: float = 15.0
    upstream_connect_timeout_s: float = 1.0

    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    # Token bucket per user. In-memory = per replica; Phase 19 moves it to Redis so the
    # limit holds across replicas.
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30

    cors_origins: list[str] = ["http://localhost:5173"]

    # ---- token exchange / delegation (ADR-019). The api owns `identity`, so it is the STS.
    db_user: str = "api_svc"
    db_password_file: Path = Field(default_factory=lambda: _secret("api_svc_password.txt"))
    db_url_override: SecretStr | None = None
    db_pool_size: int = 4
    delegation_private_key_file: Path = Field(
        default_factory=lambda: _secret("delegation_private.pem")
    )
    delegation_grant_ttl_s: int = 7200  # how long a service may act for the user (max)
    delegation_token_ttl_s: int = 300  # each delegated token; refresh re-checks the user

    @property
    def grant_ttl(self) -> timedelta:
        return timedelta(seconds=self.delegation_grant_ttl_s)

    @property
    def token_ttl(self) -> timedelta:
        return timedelta(seconds=self.delegation_token_ttl_s)

    def sts_database_url(self) -> str | None:
        """None = delegation not configured here (no db login yet): the endpoints say 503."""
        if self.db_url_override is not None:
            return self.db_url_override.get_secret_value()
        if not self.db_password_file.is_file():
            return None
        return service_database_url(
            user=self.db_user, password_file=self.db_password_file
        ).render_as_string(hide_password=False)
