"""Phase 17 (ADR-027): tokens from the OIDC IdP (Keycloak). Each test is one attack or one
mapping rule; the fake JWKS endpoint counts its calls (keys are fetched only on a new kid)."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

import jwt
import pytest

from aeoi_security.auth import AuthError
from aeoi_security.oidc import OidcConfig, OidcVerifier
from aeoi_security.rbac import Perm
from aeoi_security.testing import KeyPair, generate_keypair, service_token_for, token_for
from aeoi_web.auth import Authenticator

ISS = "http://localhost:8090/realms/aeoi"
KP = generate_keypair()
OTHER = generate_keypair()
UID = uuid4()


def jwk(kp: KeyPair, kid: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub = load_pem_public_key(kp.public_pem.encode())
    d: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
    d.update(kid=kid, use="sig", alg="RS256")
    return d


class FakeJwks:
    def __init__(self, *keys: dict[str, Any], fail: bool = False) -> None:
        self.keys = list(keys)
        self.calls = 0
        self.fail = fail

    def __call__(self, url: str, timeout_s: float) -> dict[str, Any]:
        self.calls += 1
        assert url == f"{ISS}/protocol/openid-connect/certs" and timeout_s <= 2
        if self.fail:
            raise OSError("connection refused")
        return {"keys": self.keys}


def claims(**kw: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "iss": ISS, "aud": ["aeoi-api", "account"], "sub": str(uuid4()), "iat": now,
        "exp": now + 300, "typ": "Bearer", "azp": "aeoi-console", "uid": str(UID),
        "aeoi_sub": "oidc|hana", "email": "hana@northwind.example", "name": "Hana",
        "realm_access": {"roles": ["SRE", "offline_access", "default-roles-aeoi"]},
        "groups": ["/sre", "eng-all"],
    }  # fmt: skip
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def sign(c: dict[str, Any], kp: KeyPair = KP, kid: str | None = "k1", alg: str = "RS256") -> str:
    headers = {"kid": kid} if kid else {}
    return jwt.encode(c, kp.private_pem, algorithm=alg, headers=headers)


def verifier(fetch: FakeJwks | None = None, **cfg: Any) -> OidcVerifier:
    return OidcVerifier(OidcConfig(issuer=ISS, **cfg), fetch=fetch or FakeJwks(jwk(KP, "k1")))


def test_a_keycloak_access_token_maps_to_our_principal() -> None:
    p = verifier().verify(sign(claims()))
    assert p.user_id == UID and p.subject == "oidc|hana"  # OUR directory ids, not Keycloak's
    assert p.roles == frozenset({"SRE"})  # Keycloak's default roles are dropped
    assert p.groups == frozenset({"sre", "eng-all"})  # "/sre" group path normalised
    assert p.has(Perm.ACTIONS_REQUEST) and not p.has(Perm.ACTIONS_APPROVE)
    assert not p.is_service and p.delegation is None


@pytest.mark.parametrize(
    ("why", "token"),
    [
        ("wrong issuer", sign(claims(iss="http://evil/realms/aeoi"))),
        ("wrong audience", sign(claims(aud="account"))),
        ("expired", sign(claims(exp=int(time.time()) - 120, iat=int(time.time()) - 600))),
        ("ID token, not access token", sign(claims(typ="ID"))),
        ("another client's token", sign(claims(azp="some-other-app"))),
        ("a service subject", sign(claims(aeoi_sub="service:orchestrator"))),
        ("delegated (act)", sign(claims(act={"sub": "service:orchestrator"}))),
        ("delegated (token_use)", sign(claims(token_use="delegated"))),
        ("no exp", sign(claims(exp=None))),
        ("not provisioned: no uid", sign(claims(uid=None))),
        ("not provisioned: no aeoi_sub", sign(claims(aeoi_sub=None))),
        ("signed by another key, same kid", sign(claims(), kp=OTHER)),
        ("no kid", sign(claims(), kid=None)),
    ],
)
def test_refused(why: str, token: str) -> None:
    with pytest.raises(AuthError):
        verifier().verify(token)


def test_hs256_with_the_public_key_and_alg_none_are_refused() -> None:
    """Algorithm confusion: an attacker signs with the PUBLIC key as an HMAC secret (built by
    hand - PyJWT itself refuses to produce such a token)."""
    import base64
    import hashlib
    import hmac

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "HS256", "kid": "k1", "typ": "JWT"}).encode())
    body = b64(json.dumps(claims()).encode())
    sig = hmac.new(KP.public_pem.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    v = verifier()
    with pytest.raises(AuthError):
        v.verify(f"{head}.{body}.{b64(sig)}")
    none_head = b64(json.dumps({"alg": "none", "kid": "k1"}).encode())
    with pytest.raises(AuthError):
        v.verify(f"{none_head}.{body}.")


def test_keys_are_fetched_once_and_a_new_kid_at_most_once_a_minute() -> None:
    jwks = FakeJwks(jwk(KP, "k1"))
    v = verifier(jwks)
    for _ in range(5):
        v.verify(sign(claims()))
    assert jwks.calls == 1
    for _ in range(3):  # an attacker spraying unknown kids cannot hammer the IdP
        with pytest.raises(AuthError):
            v.verify(sign(claims(), kid="rotated"))
    assert jwks.calls == 1  # within the interval: no refetch at all
    v._last_attempt -= 61  # a minute later: ONE refetch for an unknown kid
    for _ in range(3):
        with pytest.raises(AuthError):
            v.verify(sign(claims(), kid="rotated"))
    assert jwks.calls == 2
    v2 = verifier(FakeJwks(jwk(KP, "k1"), jwk(OTHER, "k2")), refetch_interval_s=0)
    assert v2.verify(sign(claims(), kp=OTHER, kid="k2")).user_id == UID  # key rotation works


def test_idp_down_is_a_401_not_a_crash() -> None:
    with pytest.raises(AuthError, match="jwks unavailable"):
        verifier(FakeJwks(fail=True)).verify(sign(claims()))


def auth(oidc: OidcVerifier | None, static_users: bool = True) -> Authenticator:
    return Authenticator(DEV.public_pem, "aeoi-dev-issuer", "aeoi-api", oidc=oidc,
                         static_users=static_users)  # fmt: skip


DEV = generate_keypair()


def test_authenticator_routes_by_issuer_and_never_retries_another_key() -> None:
    a = auth(verifier())
    assert a.verify(sign(claims())).subject == "oidc|hana"  # IdP token
    assert a.verify(token_for(DEV, "SRE")).roles == frozenset({"SRE"})  # dev token still works
    # a token CLAIMING the IdP but signed with the dev key: routed to the IdP, refused there
    with pytest.raises(AuthError):
        a.verify(sign(claims(), kp=DEV))


def test_production_mode_refuses_dev_user_tokens_but_not_service_tokens() -> None:
    a = auth(verifier(), static_users=False)
    with pytest.raises(AuthError, match="identity provider"):
        a.verify(token_for(DEV, "INCIDENT_COMMANDER"))
    assert a.verify(service_token_for(DEV, "orchestrator", "agents:run")).is_service
    assert a.verify(sign(claims())).user_id == UID


def test_a_key_the_idp_rotated_out_is_dropped() -> None:
    """Review M3: cached keys used to live until restart - a leaked, rotated-out key kept
    verifying. Now the key set is reloaded after key_max_age_s and REPLACED."""
    jwks = FakeJwks(jwk(KP, "k1"))
    v = verifier(jwks, key_max_age_s=600)
    assert v.verify(sign(claims())).user_id == UID
    jwks.keys = [jwk(OTHER, "k2")]  # the IdP rotated k1 out
    v._keys_at -= 601
    v._last_attempt -= 601
    with pytest.raises(AuthError, match="unknown signing key"):
        v.verify(sign(claims()))  # k1 is gone
    assert v.verify(sign(claims(), kp=OTHER, kid="k2")).user_id == UID


def test_idp_down_after_keys_were_loaded_keeps_the_last_keys() -> None:
    jwks = FakeJwks(jwk(KP, "k1"))
    v = verifier(jwks)
    v.verify(sign(claims()))
    jwks.fail = True
    v._keys_at -= 601
    v._last_attempt -= 601
    assert v.verify(sign(claims())).user_id == UID  # availability: last known keys [P]
    assert jwks.calls == 2


def test_no_keys_yet_retries_after_seconds_not_a_minute() -> None:
    """Review M5: Keycloak down at boot used to lock everyone out for a full minute."""
    jwks = FakeJwks(jwk(KP, "k1"), fail=True)
    v = verifier(jwks)
    with pytest.raises(AuthError, match="jwks unavailable"):
        v.verify(sign(claims()))
    jwks.fail = False
    v._last_attempt -= 6  # 6 s later
    assert v.verify(sign(claims())).user_id == UID


def test_an_idp_principal_is_marked() -> None:
    assert verifier().verify(sign(claims())).idp is True
