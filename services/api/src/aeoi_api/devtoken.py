"""DEV ONLY: mint a JWT for a seeded user.  make token ROLE=INCIDENT_COMMANDER

Reads the user's roles and groups from the identity schema (so tokens match the data),
signs with secrets/jwt_private.pem. Refuses to run with AEOI_ENV=production.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

import psycopg

from aeoi_db.config import find_repo_root, libpq_dsn
from aeoi_security.auth import issue_token

QUERY = """
SELECT u.id, u.external_subject, u.email, u.display_name,
       array_remove(array_agg(DISTINCT ur.role_name), NULL)  AS roles,
       array_remove(array_agg(DISTINCT ug.group_name), NULL) AS groups
FROM identity.users u
LEFT JOIN identity.user_roles ur ON ur.user_id = u.id
LEFT JOIN identity.user_groups ug ON ug.user_id = u.id
WHERE u.is_active AND (%(email)s::text IS NULL OR u.email = %(email)s)
GROUP BY u.id
HAVING %(role)s::text IS NULL OR %(role)s = ANY(array_agg(ur.role_name))
ORDER BY u.email
LIMIT 1
"""


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("AEOI_ENV") == "production":
        print("refusing: dev tokens are disabled in production", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--role", help="first active user with this role (e.g. SRE)")
    g.add_argument("--email", help="exact user email")
    p.add_argument("--ttl-minutes", type=int, default=60)
    args = p.parse_args(argv)

    with psycopg.connect(libpq_dsn()) as conn:
        row = conn.execute(QUERY, {"email": args.email, "role": args.role}).fetchone()
    if row is None:
        print("no matching user (did you run `make db-seed`?)", file=sys.stderr)
        return 1
    user_id, subject, email, name, roles, groups = row
    key = (find_repo_root() / "secrets" / "jwt_private.pem").read_text()
    token = issue_token(
        private_key=key,
        subject=subject,
        user_id=user_id,
        email=email,
        name=name,
        roles=list(roles),
        groups=list(groups),
        ttl=timedelta(minutes=args.ttl_minutes),
    )
    print(f"# {name} <{email}> roles={','.join(roles)} groups={','.join(groups)}", file=sys.stderr)
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
