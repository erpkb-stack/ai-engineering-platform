"""Service settings (env) + routing policy (YAML, validated at startup - fail fast)."""

from __future__ import annotations

import os
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root, service_database_url

SERVICE_DIR = Path(__file__).resolve().parents[2]


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_LLM_", extra="ignore", frozen=True)

    service_name: str = "llm-gateway"
    port: int = 8005
    db_user: str = "llm_svc"
    db_password_file: Path = Field(default_factory=lambda: _secret("llm_svc_password.txt"))
    db_url_override: SecretStr | None = None
    db_pool_size: int = 5

    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    # routing.yaml = Claude primary + Ollama fallback; routing.local.yaml = all local, $0.
    routing_file: Path = Field(default_factory=lambda: SERVICE_DIR / "config" / "routing.yaml")

    request_timeout_s: float = 120.0  # whole request incl. retries and fallback
    cache_enabled: bool = True
    cache_max_entries: int = 2000
    cache_ttl_s: int = 3600
    record_usage: bool = True

    def sqlalchemy_url(self) -> str:
        if self.db_url_override is not None:
            return self.db_url_override.get_secret_value()
        return service_database_url(
            user=self.db_user, password_file=self.db_password_file
        ).render_as_string(hide_password=False)


# ---------------------------------------------------------------- routing policy (YAML)


class ProviderKind(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPAT = "openai_compat"  # Ollama, OpenAI, Gemini (OpenAI endpoint), vLLM
    FAKE = "fake"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderConfig(_Strict):
    kind: ProviderKind
    base_url: str = ""
    hosted: bool = True  # True => prompts leave the machine => secrets are redacted first
    api_key_env: str | None = None  # name of env var holding the key
    api_key_file: str | None = None  # or a file under secrets/ (gitignored)
    timeout_s: float = 60.0
    max_concurrency: int = Field(default=4, ge=1)
    queue_timeout_s: float = 2.0
    max_attempts: int = Field(default=3, ge=1, le=6)
    breaker_threshold: int = Field(default=5, ge=1)
    breaker_cooldown_s: float = 30.0

    def api_key(self) -> str | None:
        if self.api_key_env and os.environ.get(self.api_key_env):
            return os.environ[self.api_key_env].strip()
        if self.api_key_file:
            path = _secret(self.api_key_file)
            if path.exists():
                return path.read_text().strip() or None
        return None


class ModelConfig(_Strict):
    provider: str
    # USD per 1M tokens. Third-party snapshot - VERIFY against the vendor's pricing page.
    input_per_mtok: Decimal = Decimal(0)
    output_per_mtok: Decimal = Decimal(0)
    cached_input_per_mtok: Decimal | None = None
    dimensions: int | None = None  # embedding models only
    supports_structured: bool = True


Operation = Literal["chat", "embed"]


class RouteConfig(_Strict):
    operation: Operation = "chat"
    primary: str
    fallbacks: list[str] = Field(default_factory=list)
    max_tokens_cap: int = 4096
    description: str = ""


class RoutingConfig(_Strict):
    version: Literal[1]
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    routes: dict[str, RouteConfig]
    budget_usd_per_investigation: Decimal | None = None
    pricing_note: str = ""

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        for name, m in self.models.items():
            if m.provider not in self.providers:
                raise ValueError(f"model '{name}' uses unknown provider '{m.provider}'")
        for rname, r in self.routes.items():
            chain = [r.primary, *r.fallbacks]
            for model in chain:
                if model not in self.models:
                    raise ValueError(f"route '{rname}' references unknown model '{model}'")
            if len(set(chain)) != len(chain):
                raise ValueError(f"route '{rname}' lists a model twice")
            if r.operation == "embed":
                # Vectors from different models live in different spaces. A "fallback" embedding
                # would silently corrupt the index: similarity search returns garbage, no error.
                if r.fallbacks:
                    raise ValueError(f"embed route '{rname}' must not have fallbacks")
                if self.models[r.primary].dimensions is None:
                    raise ValueError(f"embed route '{rname}': model needs 'dimensions'")
        return self

    def chain(self, route: str) -> list[str]:
        r = self.routes[route]
        return [r.primary, *r.fallbacks]


def load_routing(path: Path) -> RoutingConfig:
    raw = yaml.safe_load(path.read_text())
    return RoutingConfig.model_validate(raw)
