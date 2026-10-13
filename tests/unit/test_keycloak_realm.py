"""The generated realm's security settings (ADR-027; 17b review). Unit level: a realm change
that weakens sign-in fails here, without a running Keycloak."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("keycloak_realm", ROOT / "scripts/keycloak_realm.py")
assert spec and spec.loader
kr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kr)

ROWS = [
    ("0198fd6c-7400-782d-8b48-fe5423e816ef", "oidc|lena.ivanova", "lena@northwind.example",
     "Lena Ivanova", True, ["INCIDENT_COMMANDER", "offline_access"], ["sre"]),
    ("019907b9-2c00-7ef1-9431-1f66a04a3b01", "oidc|hana.okafor", "hana@northwind.example",
     "Hana Okafor", False, ["SRE"], []),
]  # fmt: skip


def realm() -> tuple[dict[str, Any], list[tuple[str, str]]]:
    return kr.build(ROWS)  # type: ignore[no-any-return]


def client(r: dict[str, Any], cid: str) -> dict[str, Any]:
    return next(c for c in r["clients"] if c["clientId"] == cid)


def test_the_console_is_a_public_pkce_client_with_exact_redirects() -> None:
    c = client(realm()[0], "aeoi-console")
    assert c["publicClient"] and c["standardFlowEnabled"]
    assert not c["directAccessGrantsEnabled"] and not c["implicitFlowEnabled"]
    assert c["attributes"]["pkce.code.challenge.method"] == "S256"
    assert c["redirectUris"] == ["http://localhost:5173/callback"]  # no wildcard
    assert c["attributes"]["post.logout.redirect.uris"] == "http://localhost:5173/"
    assert c["webOrigins"] == ["http://localhost:5173"]


def test_refresh_tokens_are_single_use_and_nothing_is_granted_by_default() -> None:
    r = realm()[0]
    assert r["revokeRefreshToken"] is True and r["refreshTokenMaxReuse"] == 0
    assert r["defaultRole"]["composite"] is False  # no offline_access, no account roles
    assert r["accessTokenLifespan"] == 300


def test_users_carry_our_directory_ids_and_only_our_roles() -> None:
    r, logins = realm()
    lena, hana = r["users"]
    assert lena["attributes"] == {"uid": [ROWS[0][0]], "aeoi_sub": ["oidc|lena.ivanova"]}
    assert lena["realmRoles"] == ["INCIDENT_COMMANDER"]  # offline_access dropped
    assert lena["groups"] == ["/sre"]
    assert hana["enabled"] is False
    assert [e for e, _ in logins] == ["lena@northwind.example"]  # no login for the inactive user


def test_passwords_are_stored_as_pbkdf2_hashes_only() -> None:
    r, logins = realm()
    text = json.dumps(r)
    for _, pw in logins:
        assert pw not in text
    cred = r["users"][0]["credentials"][0]
    data, secret = json.loads(cred["credentialData"]), json.loads(cred["secretData"])
    assert data == {
        "hashIterations": 210_000,
        "algorithm": "pbkdf2-sha512",
        "additionalParameters": {},
    }
    expected = hashlib.pbkdf2_hmac("sha512", logins[0][1].encode(),
                                   base64.b64decode(secret["salt"]), 210_000, dklen=64)  # fmt: skip
    assert base64.b64decode(secret["value"]) == expected  # Keycloak can verify it
