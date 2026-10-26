"""Dev user tokens for the load (Phase 27) and chaos (Phase 28) runners: the same users and
claims as `make token ROLE=... NTH=n` (aeoi_api.devtoken), minted in-process. DEV ONLY: it reads
the dev signing key the same way `make token` does; nothing is printed or written."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tokens(role: str, ttl_minutes: int = 120) -> list[str]:
    import psycopg

    from aeoi_api.devtoken import QUERY
    from aeoi_db.config import libpq_dsn
    from aeoi_security.auth import issue_token

    key = (ROOT / "secrets/jwt_private.pem").read_text()
    out = []
    with psycopg.connect(libpq_dsn()) as conn:
        for skip in range(200):
            row = conn.execute(QUERY, {"email": None, "role": role, "group": None,
                                       "without": None, "skip": skip}).fetchone()  # fmt: skip
            if row is None:
                break
            uid, sub, email, name, roles, groups = row
            out.append(issue_token(private_key=key, subject=sub, user_id=uid, email=email,
                                   name=name, roles=list(roles), groups=list(groups),
                                   ttl=timedelta(minutes=ttl_minutes)))  # fmt: skip
    return out


def running_investigations() -> int:
    """A backlog of RUNNING investigations shares the orchestrator's slots with a new run."""
    import psycopg

    from aeoi_db.config import libpq_dsn

    with psycopg.connect(libpq_dsn()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM orchestrator.investigations WHERE status = 'RUNNING'"
        ).fetchone()
    return int(row[0]) if row else 0
