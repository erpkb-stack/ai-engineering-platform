"""Delegated tokens (ADR-019): accepted only by verify_delegated_token, bound claims required."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from aeoi_security.auth import (
    DELEGATION_AUDIENCE,
    DELEGATION_ISSUER,
    AuthError,
    issue_delegated_token,
    verify_delegated_token,
    verify_token,
)
from aeoi_security.testing import delegated_token_for, generate_keypair, token_for

USER_KEYS = generate_keypair()
STS_KEYS = generate_keypair()


def test_delegated_token_round_trip_carries_binding() -> None:
    inv, inc, grant, uid = uuid4(), uuid4(), uuid4(), uuid4()
    tok = delegated_token_for(
        STS_KEYS, "SRE", investigation_id=inv, incident_id=inc, grant_id=grant, user_id=uid
    )
    p = verify_delegated_token(tok, public_key=STS_KEYS.public_pem)
    assert p.user_id == uid and not p.is_service
    assert p.delegation is not None
    assert (p.delegation.investigation_id, p.delegation.grant_id) == (inv, grant)
    assert p.delegation.incident_id == inc
    assert p.delegation.actor == "service:orchestrator"
    assert "logs:read" in {str(x) for x in p.permissions}


def test_delegated_token_is_never_a_primary_bearer() -> None:
    tok = delegated_token_for(STS_KEYS, "SRE", investigation_id=uuid4())
    with pytest.raises(AuthError):  # wrong key, issuer and audience
        verify_token(tok, public_key=STS_KEYS.public_pem)


def test_primary_verify_rejects_delegated_claims_even_with_shared_key() -> None:
    """Misconfiguration guard: same key AND forged iss/aud of user tokens still fails."""
    now = datetime.now(UTC)
    tok = jwt.encode(
        {
            "iss": "aeoi-dev-issuer",
            "aud": "aeoi-api",
            "sub": "oidc|x",
            "uid": str(uuid4()),
            "roles": ["SRE"],
            "act": {"sub": "service:orchestrator"},
            "token_use": "delegated",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        USER_KEYS.private_pem,
        algorithm="RS256",
    )
    with pytest.raises(AuthError):
        verify_token(tok, public_key=USER_KEYS.public_pem)


def test_user_token_is_not_a_delegated_token() -> None:
    with pytest.raises(AuthError):
        verify_delegated_token(token_for(USER_KEYS, "SRE"), public_key=USER_KEYS.public_pem)


@pytest.mark.parametrize("drop", ["inv", "inc", "grt", "act", "uid"])
def test_missing_binding_claim_is_rejected(drop: str) -> None:
    now = datetime.now(UTC)
    claims = {
        "iss": DELEGATION_ISSUER,
        "aud": DELEGATION_AUDIENCE,
        "sub": "oidc|x",
        "uid": str(uuid4()),
        "inv": str(uuid4()),
        "inc": str(uuid4()),
        "grt": str(uuid4()),
        "act": {"sub": "service:orchestrator"},
        "token_use": "delegated",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    del claims[drop]
    tok = jwt.encode(claims, STS_KEYS.private_pem, algorithm="RS256")
    with pytest.raises(AuthError):
        verify_delegated_token(tok, public_key=STS_KEYS.public_pem)


def test_actor_must_be_a_service_and_subject_a_user() -> None:
    now = datetime.now(UTC)
    for sub, act in (("service:agents", "service:orchestrator"), ("oidc|x", "user:evil")):
        tok = jwt.encode(
            {
                "iss": DELEGATION_ISSUER,
                "aud": DELEGATION_AUDIENCE,
                "sub": sub,
                "uid": str(uuid4()),
                "inv": str(uuid4()),
                "inc": str(uuid4()),
                "grt": str(uuid4()),
                "act": {"sub": act},
                "token_use": "delegated",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
            },
            STS_KEYS.private_pem,
            algorithm="RS256",
        )
        with pytest.raises(AuthError):
            verify_delegated_token(tok, public_key=STS_KEYS.public_pem)


def test_expired_delegated_token_is_rejected() -> None:
    tok = issue_delegated_token(
        private_key=STS_KEYS.private_pem,
        subject="oidc|x",
        user_id=uuid4(),
        email="",
        name="",
        roles=["SRE"],
        groups=[],
        actor="service:orchestrator",
        investigation_id=uuid4(),
        incident_id=uuid4(),
        grant_id=uuid4(),
        now=datetime.now(UTC) - timedelta(minutes=10),
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    with pytest.raises(AuthError):
        verify_delegated_token(tok, public_key=STS_KEYS.public_pem)


def test_issuing_an_already_expired_token_is_refused() -> None:
    with pytest.raises(ValueError, match="future"):
        issue_delegated_token(
            private_key=STS_KEYS.private_pem,
            subject="oidc|x",
            user_id=uuid4(),
            email="",
            name="",
            roles=[],
            groups=[],
            actor="service:orchestrator",
            investigation_id=uuid4(),
            incident_id=uuid4(),
            grant_id=uuid4(),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
