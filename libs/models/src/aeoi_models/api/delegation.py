"""Token exchange (ADR-019): a service trades a user's token for an investigation-bound grant
and short-lived delegated tokens. Shapes follow RFC 8693 where it has a name for the thing."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer

DELEGATION_SCOPE = "delegation:create"
TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - RFC 8693 §3


class DelegationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_token: SecretStr = Field(description="the USER's access token (never stored)")
    subject_token_type: str = Field(default=TOKEN_TYPE_ACCESS)
    investigation_id: UUID
    incident_id: UUID  # delegated tokens are bound to the incident as well (review finding)


class DelegatedToken(BaseModel):
    access_token: SecretStr
    issued_token_type: str = TOKEN_TYPE_ACCESS
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type name
    expires_in: int = Field(ge=1, description="seconds")
    expires_at: datetime
    grant_id: UUID
    grant_expires_at: datetime
    investigation_id: UUID
    incident_id: UUID

    @field_serializer("access_token", when_used="json")
    def _reveal(self, v: SecretStr) -> str:
        """JSON (the HTTP response) carries the token; repr/str/logs stay masked."""
        return v.get_secret_value()


class DelegationRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=200)


class DelegationState(BaseModel):
    grant_id: UUID
    investigation_id: UUID
    revoked: bool
    revoked_reason: str | None
    expires_at: datetime
