from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from aeoi_security import AuthError, Perm, issue_token, permissions_for, verify_token
from aeoi_security.testing import KeyPair, generate_keypair, token_for


@pytest.fixture(scope="module")
def keys() -> KeyPair:
    return generate_keypair()


def test_valid_token_gives_principal_with_permissions(keys: KeyPair) -> None:
    p = verify_token(token_for(keys, "SRE", groups=("eng-all", "sre")), public_key=keys.public_pem)
    assert p.roles == {"SRE"}
    assert p.groups == {"eng-all", "sre"}
    assert p.has(Perm.ACTIONS_REQUEST)
    assert not p.has(Perm.ACTIONS_APPROVE)
    assert p.actor == f"user:{p.user_id}"


def test_expired_token_rejected(keys: KeyPair) -> None:
    old = datetime.now(UTC) - timedelta(hours=3)
    token = issue_token(
        private_key=keys.private_pem,
        subject="s",
        user_id=uuid4(),
        email="e",
        name="n",
        roles=["SRE"],
        groups=[],
        now=old,
    )
    with pytest.raises(AuthError, match="ExpiredSignature"):
        verify_token(token, public_key=keys.public_pem)


def test_wrong_audience_and_issuer_rejected(keys: KeyPair) -> None:
    token = token_for(keys, "SRE")
    with pytest.raises(AuthError):
        verify_token(token, public_key=keys.public_pem, audience="other-api")
    with pytest.raises(AuthError):
        verify_token(token, public_key=keys.public_pem, issuer="evil-issuer")


def test_token_signed_by_another_key_rejected(keys: KeyPair) -> None:
    other = generate_keypair()
    with pytest.raises(AuthError):
        verify_token(token_for(other, "ADMIN"), public_key=keys.public_pem)


def test_alg_none_rejected(keys: KeyPair) -> None:
    claims = {
        "iss": "aeoi-dev-issuer",
        "aud": "aeoi-api",
        "sub": "x",
        "uid": str(uuid4()),
        "roles": ["ADMIN"],
        "iat": 1,
        "exp": 4102444800,
    }
    token = jwt.encode(claims, key=None, algorithm="none")
    with pytest.raises(AuthError):
        verify_token(token, public_key=keys.public_pem)


def test_hs256_key_confusion_rejected(keys: KeyPair) -> None:
    """Classic attack: sign HS256 using the PUBLIC key as the HMAC secret."""
    claims = {
        "iss": "aeoi-dev-issuer",
        "aud": "aeoi-api",
        "sub": "x",
        "uid": str(uuid4()),
        "roles": ["ADMIN"],
        "iat": 1,
        "exp": 4102444800,
    }
    try:
        token = jwt.encode(claims, keys.public_pem, algorithm="HS256")
    except jwt.InvalidKeyError:
        return  # PyJWT already refuses to use a PEM as an HMAC key - attack impossible
    with pytest.raises(AuthError):
        verify_token(token, public_key=keys.public_pem)


def test_tampered_payload_rejected(keys: KeyPair) -> None:
    header, _payload, sig = token_for(keys, "ENGINEER").split(".")
    forged = jwt.utils.base64url_encode(b'{"roles":["ADMIN"]}').decode()
    with pytest.raises(AuthError):
        verify_token(f"{header}.{forged}.{sig}", public_key=keys.public_pem)


def test_missing_uid_rejected(keys: KeyPair) -> None:
    claims = {
        "iss": "aeoi-dev-issuer",
        "aud": "aeoi-api",
        "sub": "x",
        "roles": ["SRE"],
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": 4102444800,
    }
    token = jwt.encode(claims, keys.private_pem, algorithm="RS256")
    with pytest.raises(AuthError, match="malformed"):
        verify_token(token, public_key=keys.public_pem)


def test_unknown_role_grants_nothing() -> None:
    assert permissions_for(["SUPERUSER", "root"]) == frozenset()


def test_admin_cannot_approve_actions() -> None:
    assert Perm.ACTIONS_APPROVE not in permissions_for(["ADMIN"])
