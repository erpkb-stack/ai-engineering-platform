from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root, service_database_url


class EventTransport(StrEnum):
    LOG = "log"  # default: no Kafka needed (Phases 4-17 on a laptop)
    KAFKA = "kafka"  # make up PROFILE=kafka


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_INCIDENT_", extra="ignore", frozen=True)

    service_name: str = "incident-service"
    port: int = 8001
    db_user: str = "incident_svc"
    db_password_file: Path = Field(default_factory=lambda: _secret("incident_svc_password.txt"))
    db_url_override: SecretStr | None = None  # tests / special cases only
    db_pool_size: int = 10

    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    # Phase 16 (ADR-026): an investigation's PROPOSAL arrives with the delegated token in
    # X-On-Behalf-Of (only there: require_acting_user). No file = delegated tokens refused.
    delegation_public_key_file: Path = Field(
        default_factory=lambda: _secret("delegation_public.pem")
    )
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    event_transport: EventTransport = EventTransport.LOG
    relay_enabled: bool = True
    relay_interval_s: float = 1.0
    relay_batch_size: int = 100

    # Phase 29b (ADR-044): the signed alert webhook. No secret file = the route answers 503
    # "not configured" (never accept-all). The catalog is optional: without it, triage uses
    # the label severity only and says so.
    alert_secret_file: Path = Field(default_factory=lambda: _secret("alert_webhook_secret.txt"))
    catalog_url: str | None = None  # compose: http://service-catalog:8010 when the catalog runs
    catalog_token_file: Path = Field(default_factory=lambda: _secret("incident_catalog_token.txt"))

    idempotency_ttl_hours: int = 24
    page_size_max: int = 100

    @property
    def idempotency_ttl(self) -> timedelta:
        return timedelta(hours=self.idempotency_ttl_hours)

    def sqlalchemy_url(self) -> str:
        if self.db_url_override is not None:
            return self.db_url_override.get_secret_value()
        return service_database_url(
            user=self.db_user, password_file=self.db_password_file
        ).render_as_string(hide_password=False)
