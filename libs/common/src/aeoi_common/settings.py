"""Base settings every service extends. Values come from env vars, never from code."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class BaseServiceSettings(BaseSettings):
    """Common settings. A service subclasses this and sets `env_prefix` (e.g. AEOI_API_)."""

    # populate_by_name: a field with an env alias (AEOI_ENV, AEOI_OIDC_*) can ALSO be set by
    # its name in code - without it, `Settings(environment="test")` was silently ignored
    # (extra="ignore"), found in Phase 17 when an OIDC test setting did not apply
    model_config = SettingsConfigDict(
        env_prefix="AEOI_", extra="ignore", frozen=True, populate_by_name=True
    )

    service_name: str = "aeoi-service"
    environment: Environment = Field(default=Environment.DEVELOPMENT, alias="AEOI_ENV")
    log_level: str = "INFO"
    log_json: bool = True

    # Phase 17 (ADR-027): the OIDC identity provider (Keycloak) every service trusts for USER
    # tokens. Shared env names (no service prefix): one realm for the whole platform.
    oidc_issuer: str = Field(default="", alias="AEOI_OIDC_ISSUER")  # "" = IdP not used
    oidc_audience: str = Field(default="aeoi-api", alias="AEOI_OIDC_AUDIENCE")
    oidc_jwks_url: str = Field(default="", alias="AEOI_OIDC_JWKS_URL")  # "" = Keycloak default
    oidc_clients: str = Field(default="aeoi-console", alias="AEOI_OIDC_CLIENTS")  # comma list
    # user tokens from our own static issuer (`make token`): tests, smokes, CLI. Production
    # refuses them (validator below): there, people sign in through the IdP only.
    static_user_tokens: bool = Field(default=True, alias="AEOI_STATIC_USER_TOKENS")

    database_url: SecretStr = SecretStr("postgresql+asyncpg://aeoi@localhost:5433/aeoi")
    redis_url: str = "redis://localhost:6380/0"
    # ONE broker address for every service and the CLI (.env: AEOI_KAFKA_BOOTSTRAP_SERVERS);
    # a per-service prefix here meant a service silently ignored it (Phase 18a)
    kafka_bootstrap_servers: str = Field(
        default="localhost:9094", alias="AEOI_KAFKA_BOOTSTRAP_SERVERS"
    )

    @model_validator(mode="after")
    def _production_users_come_from_the_idp(self) -> BaseServiceSettings:
        """Fail at startup, never at the first request (ADR-027 + review M1/M2)."""
        if self.environment is not Environment.PRODUCTION:
            return self
        problems = []
        if self.static_user_tokens or not self.oidc_issuer:
            problems.append("set AEOI_OIDC_ISSUER and AEOI_STATIC_USER_TOKENS=false")
        if "aeoi-cli" in {c.strip() for c in self.oidc_clients.split(",")}:
            problems.append("remove the dev-only password-grant client aeoi-cli")
        for name, url in (("AEOI_OIDC_ISSUER", self.oidc_issuer),
                          ("AEOI_OIDC_JWKS_URL", self.oidc_jwks_url)):  # fmt: skip
            if url and not url.startswith("https://"):
                problems.append(f"{name} must be https (keys over plain HTTP can be replaced)")
        if problems:
            raise ValueError("production (ADR-027): " + "; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION
