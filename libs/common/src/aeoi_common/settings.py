"""Base settings every service extends. Values come from env vars, never from code."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class BaseServiceSettings(BaseSettings):
    """Common settings. A service subclasses this and sets `env_prefix` (e.g. AEOI_API_)."""

    model_config = SettingsConfigDict(env_prefix="AEOI_", extra="ignore", frozen=True)

    service_name: str = "aeoi-service"
    environment: Environment = Field(default=Environment.DEVELOPMENT, alias="AEOI_ENV")
    log_level: str = "INFO"
    log_json: bool = True

    database_url: SecretStr = SecretStr("postgresql+asyncpg://aeoi@localhost:5433/aeoi")
    redis_url: str = "redis://localhost:6380/0"
    kafka_bootstrap_servers: str = "localhost:9094"

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION
