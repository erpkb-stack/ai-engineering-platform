"""Test helpers (keys, tokens). Safe to ship: generates throwaway keys only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from aeoi_security.auth import issue_delegated_token, issue_service_token, issue_token


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


def generate_keypair() -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return KeyPair(private, public)


def token_for(
    keys: KeyPair,
    *roles: str,
    groups: tuple[str, ...] = ("eng-all",),
    user_id: UUID | None = None,
    subject: str | None = None,
) -> str:
    uid = user_id or uuid4()
    return issue_token(
        private_key=keys.private_pem,
        subject=subject or f"oidc|test-{uid}",
        user_id=uid,
        email=f"{uid}@northwind.example",
        name="Test User",
        roles=list(roles),
        groups=list(groups),
    )


def service_token_for(keys: KeyPair, service: str, *scopes: str) -> str:
    return issue_service_token(private_key=keys.private_pem, service=service, scopes=list(scopes))


def delegated_token_for(
    keys: KeyPair,
    *roles: str,
    investigation_id: UUID,
    incident_id: UUID | None = None,
    grant_id: UUID | None = None,
    user_id: UUID | None = None,
    actor: str = "service:orchestrator",
    groups: tuple[str, ...] = ("eng-all",),
    ttl: timedelta = timedelta(minutes=5),
) -> str:
    """A delegated token signed with `keys` (use a SEPARATE pair from user tokens)."""
    uid = user_id or uuid4()
    return issue_delegated_token(
        private_key=keys.private_pem,
        subject=f"oidc|test-{uid}",
        user_id=uid,
        email=f"{uid}@northwind.example",
        name="Test User",
        roles=list(roles),
        groups=list(groups),
        actor=actor,
        investigation_id=investigation_id,
        incident_id=incident_id or uuid4(),
        grant_id=grant_id or uuid4(),
        expires_at=datetime.now(UTC) + ttl,
    )
