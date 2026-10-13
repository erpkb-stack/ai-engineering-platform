"""Generate the Keycloak realm from OUR directory (Phase 17, ADR-027). DEV ONLY.

identity.users (+ roles, groups) is the source of truth; the realm is a generated copy, never
edited by hand. Re-run after `make db-seed` (it is idempotent: same users, NEW passwords).

  uv run python scripts/keycloak_realm.py [--out secrets/keycloak]
    -> secrets/keycloak/aeoi-realm.json       imported by Keycloak at start (--import-realm)
    -> secrets/keycloak/users.txt             email + password per user (to sign in)

Each user carries two attributes the services require in the token: `uid` (identity.users.id)
and `aeoi_sub` (external_subject) - the api's token exchange matches BOTH against the
directory. They are ADMIN-ONLY (user profile): a user who could edit them could become anyone.

Clients:
- aeoi-console  public, Authorization Code + PKCE (S256), redirect http://localhost:5173/*
- aeoi-cli      DEV ONLY, password grant, for smokes/curl. Services accept its tokens only when
                AEOI_OIDC_CLIENTS lists it (never in production: ADR-027).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import psycopg

from aeoi_db.config import find_repo_root, libpq_dsn

REALM = "aeoi"
ROLES = ("ENGINEER", "SRE", "INCIDENT_COMMANDER", "MANAGER", "ADMIN")
CONSOLE_ORIGIN = "http://localhost:5173"
QUERY = """
SELECT u.id, u.external_subject, u.email, u.display_name, u.is_active,
       coalesce(array_agg(DISTINCT ur.role_name) FILTER (WHERE ur.role_name IS NOT NULL), '{}'),
       coalesce(array_agg(DISTINCT ug.group_name) FILTER (WHERE ug.group_name IS NOT NULL), '{}')
FROM identity.users u
LEFT JOIN identity.user_roles ur ON ur.user_id = u.id
LEFT JOIN identity.user_groups ug ON ug.user_id = u.id
GROUP BY u.id ORDER BY u.email
"""


def _attr_mapper(attr: str) -> dict[str, Any]:
    return {
        "name": attr, "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "config": {"user.attribute": attr, "claim.name": attr, "jsonType.label": "String",
                   "access.token.claim": "true", "id.token.claim": "false",
                   "userinfo.token.claim": "false", "multivalued": "false"},
    }  # fmt: skip


MAPPERS: list[dict[str, Any]] = [
    _attr_mapper("uid"),
    _attr_mapper("aeoi_sub"),
    {"name": "aeoi-api audience", "protocol": "openid-connect",
     "protocolMapper": "oidc-audience-mapper",
     "config": {"included.custom.audience": "aeoi-api", "access.token.claim": "true",
                "id.token.claim": "false"}},
    {"name": "groups", "protocol": "openid-connect",
     "protocolMapper": "oidc-group-membership-mapper",
     "config": {"claim.name": "groups", "full.path": "false", "access.token.claim": "true",
                "id.token.claim": "false", "userinfo.token.claim": "false"}},
]  # fmt: skip

# Keycloak 24+ declarative user profile: the defaults, plus uid/aeoi_sub that ONLY an admin
# may view or edit (a user editing their own uid would impersonate someone else)
_ADMIN_ONLY = {"view": ["admin"], "edit": ["admin"]}
USER_PROFILE = {
    "attributes": [
        {"name": "username", "displayName": "${username}",
         "permissions": {"view": ["admin", "user"], "edit": ["admin"]}, "multivalued": False},
        {"name": "email", "displayName": "${email}",
         "permissions": {"view": ["admin", "user"], "edit": ["admin"]}, "multivalued": False},
        # admin-only too (review L2): the name is quoted in approval events ("Approved by ...")
        {"name": "firstName", "displayName": "${firstName}",
         "permissions": {"view": ["admin", "user"], "edit": ["admin"]}, "multivalued": False},
        {"name": "lastName", "displayName": "${lastName}",
         "permissions": {"view": ["admin", "user"], "edit": ["admin"]}, "multivalued": False},
        {"name": "uid", "displayName": "AEOI user id", "permissions": _ADMIN_ONLY,
         "multivalued": False},
        {"name": "aeoi_sub", "displayName": "AEOI subject", "permissions": _ADMIN_ONLY,
         "multivalued": False},
    ],
    "groups": [],
}  # fmt: skip


HASH_ITERATIONS = 210_000  # Keycloak 26's own default for pbkdf2-sha512


def password_credential(password: str) -> dict[str, Any]:
    """A HASHED credential in Keycloak's import format (review L1): the realm file (mounted
    into the container, mode 644) never holds a plaintext password."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, HASH_ITERATIONS, dklen=64)
    secret = {"value": base64.b64encode(digest).decode(), "salt": base64.b64encode(salt).decode(),
              "additionalParameters": {}}  # fmt: skip
    data = {"hashIterations": HASH_ITERATIONS, "algorithm": "pbkdf2-sha512",
            "additionalParameters": {}}  # fmt: skip
    return {"type": "password", "secretData": json.dumps(secret),
            "credentialData": json.dumps(data), "temporary": False}  # fmt: skip


def build(rows: list[tuple[Any, ...]]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    users, logins, groups = [], [], set()
    for uid, subject, email, name, active, roles, grps in rows:
        password = secrets.token_urlsafe(12)
        first, _, last = str(name).partition(" ")
        groups.update(grps)
        users.append({
            "username": email, "email": email, "emailVerified": True, "enabled": bool(active),
            "firstName": first, "lastName": last or first,
            "attributes": {"uid": [str(uid)], "aeoi_sub": [subject]},
            "credentials": [password_credential(password)],
            "realmRoles": sorted(r for r in roles if r in ROLES),
            "groups": [f"/{g}" for g in sorted(grps)],
        })  # fmt: skip
        if active:
            logins.append((email, password))
    console = {
        "clientId": "aeoi-console", "name": "AEOI console", "enabled": True,
        "publicClient": True, "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
        "implicitFlowEnabled": False, "serviceAccountsEnabled": False,
        # EXACT URIs (17b review): a wildcard lets any page of the origin receive a code
        "redirectUris": [f"{CONSOLE_ORIGIN}/callback"], "webOrigins": [CONSOLE_ORIGIN],
        "attributes": {"pkce.code.challenge.method": "S256",
                       "post.logout.redirect.uris": f"{CONSOLE_ORIGIN}/"},
        "protocolMappers": MAPPERS,
    }  # fmt: skip
    cli = {
        "clientId": "aeoi-cli", "name": "AEOI CLI (dev only)", "enabled": True,
        "publicClient": True, "standardFlowEnabled": False, "directAccessGrantsEnabled": True,
        "implicitFlowEnabled": False, "serviceAccountsEnabled": False,
        "protocolMappers": MAPPERS,
    }  # fmt: skip
    realm = {
        "realm": REALM, "enabled": True, "sslRequired": "external",
        "registrationAllowed": False, "resetPasswordAllowed": False, "editUsernameAllowed": False,
        "bruteForceProtected": True,
        # a public client (the browser) holds refresh tokens: each is single-use (rotation), so a
        # stolen one dies the first time either party uses it (17b review)
        "revokeRefreshToken": True, "refreshTokenMaxReuse": 0,
        "accessTokenLifespan": 300, "ssoSessionIdleTimeout": 1800, "ssoSessionMaxLifespan": 36000,
        "roles": {"realm": [{"name": r} for r in ROLES]},
        # the default role grants NOTHING (review L2): no offline_access (30-day refresh), no
        # account-console roles - every right comes from OUR five roles
        "defaultRole": {"name": "default-roles-aeoi", "composite": False, "clientRole": False},
        "groups": [{"name": g} for g in sorted(groups)],
        "clients": [console, cli],
        "users": users,
        "components": {"org.keycloak.userprofile.UserProfileProvider": [{
            "providerId": "declarative-user-profile", "subComponents": {},
            "config": {"kc.user.profile.config": [json.dumps(USER_PROFILE)]},
        }]},
    }  # fmt: skip
    return realm, logins


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("AEOI_ENV") == "production":
        print("refusing: the generated realm is for local development only", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(find_repo_root() / "secrets" / "keycloak"))
    args = p.parse_args(argv)
    with psycopg.connect(libpq_dsn()) as conn:
        rows = conn.execute(QUERY).fetchall()
    if not rows:
        print("no users in identity.users - run `make db-seed` first", file=sys.stderr)
        return 1
    realm, logins = build(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o755)  # noqa: S103 - inside secrets/ (700); only HASHES are readable
    # the realm file is bind-mounted into the container (user 1000): 644 - it holds password
    # HASHES only. The login list (plaintext, for you) stays 600 and is never mounted.
    for name, text, mode in (("aeoi-realm.json", json.dumps(realm, indent=1), 0o644),
                             ("users.txt", "".join(f"{e} {pw}\n" for e, pw in logins), 0o600)):  # fmt: skip
        f = out / name
        f.write_text(text)
        os.chmod(f, mode)
    print(f"realm '{REALM}': {len(realm['users'])} users ({len(logins)} active), "
          f"{len(realm['groups'])} groups -> {out}/aeoi-realm.json, logins in {out}/users.txt")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
