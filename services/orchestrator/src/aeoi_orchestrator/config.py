"""orchestrator settings (env prefix AEOI_ORCH_)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root, service_database_url


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_ORCH_", extra="ignore", frozen=True)

    service_name: str = "orchestrator"
    db_user: str = "orch_svc"
    db_password_file: Path = Field(default_factory=lambda: _secret("orch_svc_password.txt"))
    db_url_override: SecretStr | None = None
    db_pool_size: int = 4

    incident_service_url: str = "http://localhost:8001"
    agents_url: str = "http://localhost:8003"
    # scopes agents:run + evidence:write (make orchestrator-token)
    service_token_file: Path = Field(
        default_factory=lambda: _secret("orchestrator_service_token.txt")
    )
    # look-back/look-ahead around detected_at when the caller gives no window
    window_before_min: int = 60
    window_after_min: int = 30
    budget_usd: Decimal = Decimal("0.50")
    task_deadline_s: float = 180.0  # > agents' LLM timeout (90 s) + tool calls

    def sqlalchemy_url(self) -> str:
        if self.db_url_override is not None:
            return self.db_url_override.get_secret_value()
        return service_database_url(
            user=self.db_user, password_file=self.db_password_file
        ).render_as_string(hide_password=False)
