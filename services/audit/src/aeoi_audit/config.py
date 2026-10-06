"""audit settings (env prefix AEOI_AUDIT_)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from aeoi_common.settings import BaseServiceSettings
from aeoi_db.config import find_repo_root, service_database_url


def _secret(name: str) -> Path:
    return find_repo_root() / "secrets" / name


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="AEOI_AUDIT_", extra="ignore", frozen=True)

    service_name: str = "audit"
    port: int = 8008
    db_user: str = "audit_svc"
    db_password_file: Path = Field(default_factory=lambda: _secret("audit_svc_password.txt"))
    db_url_override: SecretStr | None = None
    db_pool_size: int = 5

    jwt_public_key_file: Path = Field(default_factory=lambda: _secret("jwt_public.pem"))
    jwt_issuer: str = "aeoi-dev-issuer"
    jwt_audience: str = "aeoi-api"

    # reads are time-bounded so a query touches at most ~2 monthly partitions
    max_query_window_days: int = 31

    def sqlalchemy_url(self) -> str:
        if self.db_url_override is not None:
            return self.db_url_override.get_secret_value()
        return service_database_url(
            user=self.db_user, password_file=self.db_password_file
        ).render_as_string(hide_password=False)
