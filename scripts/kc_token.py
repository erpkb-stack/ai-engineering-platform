"""Print a Keycloak ACCESS token for the n-th user with a role (Phase 17, ADR-027). DEV ONLY.

Uses the generated realm (secrets/keycloak/aeoi-realm.json: who has which role) and its login
list (users.txt), and the dev-only `aeoi-cli` client (password grant). Same selection order as
`make token ROLE=X NTH=n` (users sorted by email), so `NTH=2` is the same second IC.

  export KT=$(make -s kc-token ROLE=INCIDENT_COMMANDER NTH=2)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from aeoi_db.config import find_repo_root


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("AEOI_ENV") == "production":
        print("refusing: password-grant tokens are for local development only", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", required=True)
    p.add_argument("--nth", type=int, default=1)
    p.add_argument("--issuer", default=os.environ.get("AEOI_OIDC_ISSUER", ""))
    args = p.parse_args(argv)
    if not args.issuer:
        print("set AEOI_OIDC_ISSUER (see .env.example)", file=sys.stderr)
        return 2
    kc = find_repo_root() / "secrets" / "keycloak"
    try:
        realm = json.loads((kc / "aeoi-realm.json").read_text())
        logins = dict(line.split(" ", 1) for line in (kc / "users.txt").read_text().splitlines())
    except OSError:
        print("no generated realm - run `make keycloak-up`", file=sys.stderr)
        return 1
    users = sorted(u["email"] for u in realm["users"]
                   if u["enabled"] and args.role in u["realmRoles"])  # fmt: skip
    if len(users) < args.nth:
        print(f"no user #{args.nth} with role {args.role}", file=sys.stderr)
        return 1
    email = users[args.nth - 1]
    try:  # 30 s: the FIRST login on a fresh Keycloak (cold JVM + PBKDF2) took >10 s on the Mac
        r = httpx.post(f"{args.issuer.rstrip('/')}/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "aeoi-cli", "username": email,
            "password": logins[email].strip(),
        }, timeout=30, trust_env=False)  # fmt: skip
    except httpx.HTTPError as e:
        print(f"keycloak not answering at {args.issuer}: {type(e).__name__}", file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"keycloak refused {email}: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return 1
    print(f"# {email} role={args.role} (Keycloak)", file=sys.stderr)
    print(r.json()["access_token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
