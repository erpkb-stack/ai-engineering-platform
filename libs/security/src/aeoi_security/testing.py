"""Test helpers (keys, tokens). Safe to ship: generates throwaway keys only."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from aeoi_security.auth import issue_token


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
