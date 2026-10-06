from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_API_", extra="ignore", frozen=True)

    service_name: str = "api"
    port: int = 8000
    incident_service_url: str = "http://localhost:8001"
    rag_service_url: str = "http://localhost:8004"
    upstream_timeout_s: float = 5.0
    # search may include an LLM rerank (25 s budget in rag) - a longer, explicit timeout
    rag_timeout_s: float = 35.0
    upstream_connect_timeout_s: float = 1.0

    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    # Token bucket per user. In-memory = per replica; Phase 19 moves it to Redis so the
    # limit holds across replicas.
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30

    cors_origins: list[str] = ["http://localhost:5173"]
