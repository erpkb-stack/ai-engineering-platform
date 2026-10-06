"""DEV ONLY: mint a JWT for a seeded user.  make token ROLE=INCIDENT_COMMANDER
Or a SERVICE token (no DB needed):        make service-token SERVICE=orchestrator SCOPES=llm:invoke

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
from aeoi_security.auth import issue_service_token, issue_token

QUERY = """
SELECT u.id, u.external_subject, u.email, u.display_name,
       array_remove(array_agg(DISTINCT ur.role_name), NULL)  AS roles,
       array_remove(array_agg(DISTINCT ug.group_name), NULL) AS groups
FROM identity.users u
LEFT JOIN identity.user_roles ur ON ur.user_id = u.id
LEFT JOIN identity.user_groups ug ON ug.user_id = u.id
WHERE u.is_active AND (%(email)s::text IS NULL OR u.email = %(email)s)
GROUP BY u.id
HAVING (%(role)s::text IS NULL OR %(role)s = ANY(array_agg(ur.role_name)))
   AND (%(group)s::text IS NULL OR %(group)s = ANY(array_agg(ug.group_name)))
   AND (%(without)s::text IS NULL OR NOT (%(without)s = ANY(array_agg(ug.group_name))))
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
    p.add_argument("--group", help="...who is in this group (e.g. security-team)")
    p.add_argument("--without-group", help="...who is NOT in this group (outsider tests)")
    g.add_argument("--service", help="service name -> token with sub=service:<name>")
    p.add_argument("--scope", action="append", default=[], help="service scope (repeatable)")
    p.add_argument("--ttl-minutes", type=int, default=60)
    args = p.parse_args(argv)

    key_file = find_repo_root() / "secrets" / "jwt_private.pem"
    if args.service:
        if not args.scope:
            print("a service token needs at least one --scope", file=sys.stderr)
            return 2
        print(f"# service:{args.service} scopes={','.join(args.scope)}", file=sys.stderr)
        print(
            issue_service_token(
                private_key=key_file.read_text(),
                service=args.service,
                scopes=args.scope,
                ttl=timedelta(minutes=args.ttl_minutes),
            )
        )
        return 0

    with psycopg.connect(libpq_dsn()) as conn:
        row = conn.execute(
            QUERY,
            {
                "email": args.email,
                "role": args.role,
                "group": args.group,
                "without": args.without_group,
            },
        ).fetchone()
    if row is None:
        print("no matching user (did you run `make db-seed`?)", file=sys.stderr)
        return 1
    user_id, subject, email, name, roles, groups = row
    key = key_file.read_text()
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
