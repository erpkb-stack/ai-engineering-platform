"""tool-gateway settings (env prefix AEOI_TOOLS_)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root, service_database_url


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


def _here(*parts: str) -> Path:
    return find_repo_root().joinpath(*parts)


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_TOOLS_", extra="ignore", frozen=True)

    service_name: str = "tool-gateway"
    port: int = 8006
    db_user: str = "tools_svc"
    db_password_file: Path = Field(default_factory=lambda: _secret("tools_svc_password.txt"))
    db_url_override: SecretStr | None = None
    db_pool_size: int = 10

    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    # ADR-019: delegated tokens are accepted in the on-behalf-of slot only if this file exists
    delegation_public_key_file: Path = Field(
        default_factory=lambda: _secret("delegation_public.pem")
    )
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    agents_file: Path = Field(
        default_factory=lambda: _here("services", "tool-gateway", "config", "agents.yaml")
    )
    catalog_file: Path = Field(default_factory=lambda: _here("data", "generated", "catalog.json"))
    # Phase 32a: AEOI's own services, read on top (in the image: config/ ships with the service)
    catalog_extra_files: list[Path] = Field(
        default_factory=lambda: [_here("services", "tool-gateway", "config", "catalog-aeoi.json")]
    )
    # Phase 20 (ADR-032): "" = the JSON file above; set = the Java service-catalog (same tool
    # contract): AEOI_TOOLS_CATALOG_URL=http://localhost:8010. Its host must be in the allow-list.
    catalog_url: str = ""
    catalog_token_file: Path = Field(default_factory=lambda: _secret("tools_catalog_token.txt"))
    # Phase 32a: "" = devdata (seeded); set = a real Prometheus for query_metrics (same tool
    # contract): AEOI_TOOLS_METRICS_URL=http://localhost:9090. Its host must be in the
    # allow-list. Only 3 metrics have a real source; the others answer "no series".
    metrics_url: str = ""
    # Phase 32a: the same for search_logs from a real Loki: AEOI_TOOLS_LOGS_URL=http://localhost:3100
    # (lines are event names only - logs carry no message text, ADR-035)
    logs_url: str = ""
    # catalog service_key -> OTel service.name, where they differ (unmapped = the same name)
    otel_service_names: dict[str, str] = {}
    rag_url: str = "http://localhost:8004"
    # ADR-020: the gateway's identity towards rag (scope rag:obo), always WITH the user's token
    rag_token_file: Path = Field(default_factory=lambda: _secret("tools_rag_token.txt"))
    # host:port the HTTP-backed tools may reach. Nothing else, ever (no proxies either).
    egress_allowlist: list[str] = ["localhost:8004", "localhost:8010"]
    http_timeout_s: float = 10.0

    # Phase 16 (ADR-026): approvals are verified + consumed at incident-service (scope
    # approvals:consume, WITH the acting user's own token); the rollback writes through the
    # insert-only CD login (cd_svc). A missing token/login = consequential tools refused.
    incident_service_url: str = "http://localhost:8001"
    approvals_token_file: Path = Field(default_factory=lambda: _secret("tools_incident_token.txt"))
    cd_db_user: str = "cd_svc"
    cd_db_password_file: Path = Field(default_factory=lambda: _secret("cd_svc_password.txt"))
    cd_db_url_override: SecretStr | None = None

    # per (user, tool) token bucket. [P] in memory; [Prod] Redis (Phase 19)
    rate_limit_per_minute: int = 60
    rate_limit_burst: int = 20
    # per dependency: bounded concurrency + breaker
    bulkhead: dict[str, int] = {"devdata": 8, "rag": 4, "catalog": 16, "deployer": 1,
                                "prometheus": 4, "loki": 4}  # fmt: skip
    breaker_threshold: int = 5
    breaker_cooldown_s: float = 30.0
    max_request_bytes: int = 64 * 1024

    # audit relay (tools.audit_outbox -> audit service)
    relay_enabled: bool = True
    audit_url: str = "http://localhost:8008"
    audit_token_file: Path = Field(default_factory=lambda: _secret("tools_audit_token.txt"))
    relay_interval_s: float = 1.0
    relay_batch_size: int = 200

    def cd_sqlalchemy_url(self) -> str | None:
        if self.cd_db_url_override is not None:
            return self.cd_db_url_override.get_secret_value()
        if not self.cd_db_password_file.is_file():
            return None  # no login yet (make db-users): rollback refuses, nothing else changes
        return service_database_url(
            user=self.cd_db_user, password_file=self.cd_db_password_file
        ).render_as_string(hide_password=False)

    def sqlalchemy_url(self) -> str:
        if self.db_url_override is not None:
            return self.db_url_override.get_secret_value()
        return service_database_url(
            user=self.db_user, password_file=self.db_password_file
        ).render_as_string(hide_password=False)
